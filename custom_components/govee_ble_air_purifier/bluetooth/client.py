"""BLE client for H712-family Govee air purifiers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import logging
import time
from typing import Any

from ..models import NightLightState, PurifierState
from ..profiles import EncryptionMode, H7124_PROFILE, ModelProfile, NightLightProfile
from ..protocol import (
    decode_night_light_power_brightness,
    decode_night_light_rgb_state,
    is_command_echo,
    is_fan_mode_confirmation,
    is_night_light_brightness_confirmation,
    is_night_light_power_brightness_response,
    is_night_light_power_confirmation,
    is_night_light_rgb_state_response,
    is_power_confirmation,
)
from . import GoveeBleClientError, GoveeBleDisconnectedError, transport
from .framing import ProtocolError, validate_frame
from .govee_v1 import (
    COMMUNICATION_KEY,
    build_handshake_request,
    decrypt_frame,
    encrypt_frame,
    identify_handshake_frame,
    parse_session_key,
    validate_handshake_confirmation,
)
from .transport import _async_wait_until

DEFAULT_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0
COMMAND_CONFIRMATION_TIMEOUT = 2.0
HANDSHAKE_TIMEOUT = 10.0
CONNECTION_IDLE_GRACE = 5.0
MAX_CONNECTION_IDLE_TIMEOUT = 30.0
DISCONNECT_TIMEOUT = 5.0
CONNECTION_LEASE_TIMEOUT = (
    transport.CONNECTION_TIMEOUT + HANDSHAKE_TIMEOUT + POLL_TIMEOUT + DISCONNECT_TIMEOUT
)
MAX_PRIORITY_COMMAND_BURST = 3
ADVERTISEMENT_RETRY_DELAYS = (60.0, 120.0, 300.0)
_LOGGER = logging.getLogger(__name__)
_ABANDONED_OPERATION_FUTURES: set[asyncio.Future[Any]] = set()


def _observe_abandoned_operation(future: asyncio.Future[Any]) -> None:
    """Release and retrieve a helper-owned future after its eventual exit."""

    _ABANDONED_OPERATION_FUTURES.discard(future)
    if future.cancelled():
        return
    try:
        future.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return


def _cancel_and_observe(future: asyncio.Future[Any]) -> None:
    """Request cancellation without waiting indefinitely for acknowledgement."""

    if future.done():
        _observe_abandoned_operation(future)
        return
    _ABANDONED_OPERATION_FUTURES.add(future)
    future.add_done_callback(_observe_abandoned_operation)
    future.cancel()


def connection_idle_timeout_for_polling_interval(
    polling_interval_seconds: float,
) -> float:
    """Retain through the next poll or release after a short activity grace."""

    next_poll_timeout = polling_interval_seconds + CONNECTION_IDLE_GRACE
    if next_poll_timeout <= MAX_CONNECTION_IDLE_TIMEOUT:
        return next_poll_timeout
    return CONNECTION_IDLE_GRACE


class _ConnectionLeasePriority(Enum):
    """Scheduling priority for integration-wide Bluetooth work."""

    COMMAND = "command"
    POLL = "poll"


@dataclass(slots=True)
class _ConnectionLeaseWaiter:
    """One task waiting for the integration-wide Bluetooth lease."""

    client: GoveeBleClient
    priority: _ConnectionLeasePriority
    future: asyncio.Future[None]
    granted: bool = False


class GoveeConnectionArbiter:
    """Share one retained GATT connection across purifier entries."""

    def __init__(self) -> None:
        self._lease_held = False
        self._owner: GoveeBleClient | None = None
        self._command_waiters: deque[_ConnectionLeaseWaiter] = deque()
        self._poll_waiters: deque[_ConnectionLeaseWaiter] = deque()
        self._consecutive_priority_commands = 0

    async def async_run(
        self,
        client: GoveeBleClient,
        operation: Callable[[], Awaitable[Any]],
        deadline: float | None = None,
        *,
        priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> Any:
        """Run one client's work after releasing a different idle owner."""

        await self.async_acquire(client, deadline, priority=priority)
        try:
            return await operation()
        finally:
            self.release()

    async def async_acquire(
        self,
        client: GoveeBleClient,
        deadline: float | None = None,
        *,
        priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> None:
        """Acquire the shared lease before a client transaction lock."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        queue_deadline = deadline or (started + CONNECTION_LEASE_TIMEOUT)
        waiter = _ConnectionLeaseWaiter(client, priority, loop.create_future())
        _LOGGER.debug(
            "%s waiting for %s shared BLE connection lease",
            client._log_label,
            priority.value,
        )
        # Each holder gets a separate GATT deadline only after it reaches the
        # front of this queue.  The queue itself must still be bounded so an
        # unavailable peer cannot leave Home Assistant setup initializing.
        if not self._lease_held:
            self._lease_held = True
            waiter.granted = True
        else:
            self._queue_for(priority).append(waiter)
        try:
            if not waiter.granted:
                await _async_wait_until(asyncio.shield(waiter.future), queue_deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._cancel_waiter(waiter)
            _LOGGER.debug(
                "%s timed out waiting for %s shared BLE connection lease after "
                "%.2f seconds",
                client._log_label,
                priority.value,
                loop.time() - started,
            )
            raise GoveeBleClientError(
                "Timed out waiting for another purifier's Bluetooth connection"
            ) from err
        except BaseException:
            self._cancel_waiter(waiter)
            raise
        try:
            owner = self._owner
            if owner is not None and owner is not client:
                await owner._async_release_for_connection_switch(
                    loop.time() + DISCONNECT_TIMEOUT
                )
            self._owner = client
            _LOGGER.debug(
                "%s acquired %s shared BLE connection lease after %.2f seconds",
                client._log_label,
                priority.value,
                loop.time() - started,
            )
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """Release a lease acquired by :meth:`async_acquire`."""

        if not self._lease_held:
            raise RuntimeError("Shared BLE connection lease is not held")
        waiter = self._next_waiter()
        if waiter is None:
            self._lease_held = False
            return
        waiter.granted = True
        waiter.future.set_result(None)

    def _queue_for(
        self, priority: _ConnectionLeasePriority
    ) -> deque[_ConnectionLeaseWaiter]:
        """Return the FIFO queue for one class of Bluetooth work."""

        if priority is _ConnectionLeasePriority.COMMAND:
            return self._command_waiters
        return self._poll_waiters

    def _cancel_waiter(self, waiter: _ConnectionLeaseWaiter) -> None:
        """Remove or relinquish a waiter after timeout or cancellation."""

        if waiter.granted:
            self.release()
            return
        queue = self._queue_for(waiter.priority)
        try:
            queue.remove(waiter)
        except ValueError:
            return
        waiter.future.cancel()

    def _next_waiter(self) -> _ConnectionLeaseWaiter | None:
        """Select the next waiter, prioritizing commands without starving polls."""

        if self._command_waiters and self._poll_waiters:
            if self._consecutive_priority_commands >= MAX_PRIORITY_COMMAND_BURST:
                self._consecutive_priority_commands = 0
                return self._poll_waiters.popleft()
            self._consecutive_priority_commands += 1
            return self._command_waiters.popleft()
        if self._command_waiters:
            self._consecutive_priority_commands = 0
            return self._command_waiters.popleft()
        if self._poll_waiters:
            self._consecutive_priority_commands = 0
            return self._poll_waiters.popleft()
        self._consecutive_priority_commands = 0
        return None

    def connection_released(self, client: GoveeBleClient) -> None:
        """Forget an owner after its retained connection is gone."""

        if self._owner is client:
            self._owner = None


class GoveeBleClient:
    """Small serialized request/response BLE client."""

    def __init__(
        self,
        hass: Any,
        address: str,
        *,
        profile: ModelProfile = H7124_PROFILE,
        polling_interval_seconds: float | None = None,
        connection_arbiter: GoveeConnectionArbiter | None = None,
    ) -> None:
        self._hass = hass
        self._address = address
        self._profile = profile
        if polling_interval_seconds is None:
            polling_interval_seconds = profile.polling_interval_seconds
        device_id = transport.device_log_id(address)
        self._log_label = f"{profile.model} [{device_id}]"
        self._connection_idle_timeout = connection_idle_timeout_for_polling_interval(
            polling_interval_seconds
        )
        self._connection_arbiter = connection_arbiter
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._disconnect_signal: asyncio.Event | None = None
        self._abandoned_connection_operations: set[asyncio.Future[Any]] = set()
        self._session_key: bytes | None = None
        self._connected_at: float | None = None
        self._session_started_at: float | None = None
        self._idle_disconnect_handle: asyncio.TimerHandle | None = None
        self._idle_disconnect_task: asyncio.Task[Any] | None = None
        self._fresh_advertisement_after: float | None = None
        self._advertisement_retry_at = 0.0
        self._advertisement_failure_count = 0
        self._unexpected_disconnect_revision = 0
        self._closed = False

    async def async_get_state(self) -> PurifierState:
        """Poll power, PM2.5, and filter-life state."""

        attempt = 0
        while True:
            try:
                requests = (
                    (
                        self._profile.state_query_command,
                        self._profile.is_power_state_response,
                    ),
                    (
                        self._profile.status_query_command,
                        self._profile.is_status_response,
                    ),
                )
                optional_requests: tuple[
                    tuple[bytes, Callable[[bytes], bool]], ...
                ] = ()
                night_light = self._profile.night_light
                night_light_poll_timeout = (
                    night_light.poll_timeout_seconds if night_light is not None else 0.0
                )
                poll_night_light = night_light_poll_timeout > 0
                if poll_night_light:
                    assert night_light is not None
                    optional_requests = (
                        (
                            night_light.power_brightness_query_command,
                            is_night_light_power_brightness_response,
                        ),
                        (
                            night_light.rgb_state_query_command,
                            is_night_light_rgb_state_response,
                        ),
                    )
                frames = await self._async_write_and_wait_many(
                    requests,
                    timeout=POLL_TIMEOUT,
                    optional_requests=optional_requests,
                    optional_timeout=night_light_poll_timeout,
                    lease_priority=_ConnectionLeasePriority.POLL,
                )
                power_frame, status_frame = frames[:2]
                assert power_frame is not None and status_frame is not None
                status = self._profile.decode_status(status_frame)
                night_light_state = None
                if poll_night_light:
                    power_brightness_frame, rgb_frame = frames[2:]
                    if power_brightness_frame is not None or rgb_frame is not None:
                        power_brightness = (
                            decode_night_light_power_brightness(
                                power_brightness_frame
                            )
                            if power_brightness_frame is not None
                            else NightLightState()
                        )
                        night_light_state = NightLightState(
                            is_on=power_brightness.is_on,
                            brightness_percent=power_brightness.brightness_percent,
                            rgb_color=(
                                decode_night_light_rgb_state(rgb_frame)
                                if rgb_frame is not None
                                else None
                            ),
                        )
                return PurifierState(
                    is_on=self._profile.decode_power_state(power_frame),
                    pm25=status.pm25,
                    filter_life=status.filter_life,
                    night_light=night_light_state,
                )
            except GoveeBleDisconnectedError:
                if attempt:
                    raise
                attempt += 1
                _LOGGER.debug(
                    "%s retrying read-only poll after BLE disconnection",
                    self._log_label,
                )

    async def async_set_power(self, is_on: bool) -> bool:
        """Set purifier power."""

        command = (
            self._profile.power_on_command if is_on else self._profile.power_off_command
        )
        frame = await self._async_write_and_wait(
            command,
            lambda frame: is_power_confirmation(frame, is_on),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        return self._profile.decode_power_state(frame)

    async def async_set_fan_mode(self, mode: str) -> str:
        """Set purifier fan mode using its model profile."""

        try:
            command = self._profile.fan_mode_commands[mode]
        except KeyError as err:
            raise ValueError(f"Unsupported fan mode: {mode}") from err
        await self._async_write_and_wait(
            command,
            lambda frame: is_fan_mode_confirmation(frame, mode, command),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        return mode

    async def async_set_night_light_power(self, is_on: bool) -> NightLightState:
        """Set night-light power and return its normalized state report."""

        profile = self._require_night_light_profile()
        command = profile.power_on_command if is_on else profile.power_off_command
        frame = await self._async_write_and_wait(
            command,
            lambda frame: is_night_light_power_confirmation(frame, is_on),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        return decode_night_light_power_brightness(frame)

    async def async_set_night_light_brightness(
        self, brightness_percent: int
    ) -> NightLightState:
        """Set night-light brightness and return its normalized state report."""

        command = self._require_night_light_profile().build_brightness_command(
            brightness_percent
        )
        frame = await self._async_write_and_wait(
            command,
            lambda frame: is_night_light_brightness_confirmation(
                frame, brightness_percent
            ),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        return decode_night_light_power_brightness(frame)

    async def async_set_night_light_rgb(
        self, rgb_color: tuple[int, int, int]
    ) -> NightLightState:
        """Set night-light RGB and return its command-confirmed color."""

        command = self._require_night_light_profile().build_rgb_command(rgb_color)
        await self._async_write_and_wait(
            command,
            lambda frame: is_command_echo(frame, command),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        return NightLightState(rgb_color=rgb_color)

    async def async_set_power_and_fan_mode(self, mode: str) -> PurifierState:
        """Power on and set fan mode in one serialized BLE connection."""

        try:
            mode_command = self._profile.fan_mode_commands[mode]
        except KeyError as err:
            raise ValueError(f"Unsupported fan mode: {mode}") from err
        power_frame, _mode_frame = await self._async_write_and_wait_many(
            (
                (
                    self._profile.power_on_command,
                    lambda frame: is_power_confirmation(frame, True),
                ),
                (
                    mode_command,
                    lambda frame: is_fan_mode_confirmation(frame, mode, mode_command),
                ),
            ),
            timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        if power_frame is None:  # Mandatory requests always produce a frame.
            raise GoveeBleClientError("Purifier response was unavailable")
        return PurifierState(
            is_on=self._profile.decode_power_state(power_frame),
            fan_mode=mode,
        )

    def _require_night_light_profile(self) -> NightLightProfile:
        """Return the configured night-light capability or reject the command."""

        if self._profile.night_light is None:
            raise ValueError("This purifier profile has no night-light capability")
        return self._profile.night_light

    async def _async_write_without_response(self, command: bytes) -> None:
        await self._async_write_commands_without_response((command,))

    async def _async_write_commands_without_response(
        self, commands: tuple[bytes, ...]
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = 0.0

        async def operation(client: Any) -> None:
            nonlocal deadline
            deadline = loop.time() + DEFAULT_TIMEOUT
            disconnect_signal = self._disconnect_signal_for(client)
            try:
                for command in commands:
                    await self._async_wait_for_connection(
                        client.write_gatt_char(
                            self._profile.write_char_uuid,
                            self._encode_application_frame(command),
                            response=False,
                        ),
                        disconnect_signal,
                        deadline,
                    )
            except (TimeoutError, asyncio.TimeoutError) as err:
                raise GoveeBleClientError("Timed out writing purifier request") from err

        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect()
        await self._async_acquire_connection_lease()
        try:
            await self._async_prepare_connection()
            deadline = loop.time() + DEFAULT_TIMEOUT
            try:
                await _async_wait_until(self._lock.acquire(), deadline)
            except (TimeoutError, asyncio.TimeoutError) as err:
                raise GoveeBleClientError(
                    "Timed out waiting for BLE transaction lock"
                ) from err
            try:
                if self._connection_arbiter is None:
                    await self._async_with_connection(operation)
                else:
                    await self._async_with_connection_unarbitrated(
                        operation,
                        deadline=loop.time() + transport.CONNECTION_TIMEOUT,
                    )
            finally:
                self._lock.release()
        finally:
            self._release_connection_lease()

    async def _async_write_and_wait(
        self,
        command: bytes,
        matcher: Callable[[bytes], bool],
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> bytes:
        frames = await self._async_write_and_wait_many(
            ((command, matcher),), timeout=timeout
        )
        frame = frames[0]
        if frame is None:  # Mandatory requests always produce a frame.
            raise GoveeBleClientError("Purifier response was unavailable")
        return frame

    async def _async_write_and_wait_many(
        self,
        requests: tuple[tuple[bytes, Callable[[bytes], bool]], ...],
        *,
        timeout: float = DEFAULT_TIMEOUT,
        optional_requests: tuple[
            tuple[bytes, Callable[[bytes], bool]], ...
        ] = (),
        optional_timeout: float = 0.0,
        lease_priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> tuple[bytes | None, ...]:
        loop = asyncio.get_running_loop()
        total_request_count = len(requests) + len(optional_requests)
        stage = "waiting for transaction lock"
        _LOGGER.debug(
            "%s BLE transaction started with %d requests (%.2f second timeout)",
            self._log_label,
            total_request_count,
            timeout,
        )
        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect()
        await self._async_acquire_connection_lease(lease_priority)
        try:
            await self._async_prepare_connection()
            started = loop.time()
            deadline = started + timeout
            self._log_stage("BLE transaction", stage, started, deadline)
            try:
                await _async_wait_until(self._lock.acquire(), deadline)
            except (TimeoutError, asyncio.TimeoutError) as err:
                self._log_timeout("BLE transaction", stage, started)
                raise GoveeBleClientError(
                    "Timed out waiting for BLE transaction lock"
                ) from err
            frames: list[bytes | None] = [None] * total_request_count
            future: asyncio.Future[bytes] | None = None
            optional_futures: list[asyncio.Future[bytes]] = []
            collecting_optional = False
            initiated_optional_count = 0
            discard_connection = False
            current_request_index = 0
            request_started: float | None = None
            notification_count = 0
            ignored_handshake_count = 0
            nonmatching_notification_count = 0

            def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
                nonlocal collecting_optional
                nonlocal future
                nonlocal ignored_handshake_count
                nonlocal nonmatching_notification_count
                nonlocal notification_count
                if future is None and not collecting_optional:
                    return
                notification_count += 1
                session_key_available = self._session_key is not None
                try:
                    frame = self._decode_application_frame(bytes(data))
                except ProtocolError as err:
                    if (
                        self._profile.encryption is EncryptionMode.GOVEE_V1
                        and session_key_available
                    ):
                        handshake_command = identify_handshake_frame(bytes(data))
                        if handshake_command is not None:
                            ignored_handshake_count += 1
                            _LOGGER.debug(
                                "%s Govee V1 application decryption diagnostic: "
                                "ignored valid late e7 %02x handshake notification",
                                self._log_label,
                                handshake_command,
                            )
                            return
                        _LOGGER.debug(
                            "%s Govee V1 application decryption diagnostic: "
                            "not a valid late e7 01/e7 02 handshake notification",
                            self._log_label,
                        )
                    if collecting_optional:
                        nonmatching_notification_count += 1
                    elif future is not None and not future.done():
                        future.set_exception(err)
                    return
                try:
                    validate_frame(frame)
                except ProtocolError as err:
                    if collecting_optional:
                        nonmatching_notification_count += 1
                    elif future is not None and not future.done():
                        future.set_exception(err)
                    return

                if collecting_optional:
                    for index, ((_command, matcher), optional_future) in enumerate(
                        zip(
                            optional_requests[:initiated_optional_count],
                            optional_futures[:initiated_optional_count],
                            strict=True,
                        ),
                        start=len(requests),
                    ):
                        if optional_future.done() or not matcher(frame):
                            continue
                        frames[index] = frame
                        optional_future.set_result(frame)
                        return
                    nonmatching_notification_count += 1
                    return

                matcher = requests[current_request_index - 1][1]
                if not matcher(frame):
                    nonmatching_notification_count += 1
                    return
                if future is not None and not future.done():
                    future.set_result(frame)

            async def operation(client: Any) -> tuple[bytes | None, ...]:
                nonlocal collecting_optional
                nonlocal current_request_index
                nonlocal deadline, discard_connection, future
                nonlocal initiated_optional_count
                nonlocal ignored_handshake_count
                nonlocal nonmatching_notification_count
                nonlocal notification_count, request_started, stage, started
                started = loop.time()
                deadline = started + timeout
                disconnect_signal = self._disconnect_signal_for(client)
                primary_error: BaseException | None = None
                try:
                    stage = "starting application notifications"
                    self._log_stage("BLE transaction", stage, started, deadline)
                    await self._async_wait_for_connection(
                        client.start_notify(
                            self._profile.notify_char_uuid, notification_handler
                        ),
                        disconnect_signal,
                        deadline,
                    )
                    for index, (command, _matcher) in enumerate(requests, start=1):
                        future = loop.create_future()
                        current_request_index = index
                        request_started = loop.time()
                        notification_count = 0
                        ignored_handshake_count = 0
                        nonmatching_notification_count = 0
                        stage = f"writing request {index}/{total_request_count}"
                        self._log_stage("BLE transaction", stage, started, deadline)
                        await self._async_wait_for_connection(
                            client.write_gatt_char(
                                self._profile.write_char_uuid,
                                self._encode_application_frame(command),
                                response=False,
                            ),
                            disconnect_signal,
                            deadline,
                        )
                        _LOGGER.debug(
                            "%s BLE request %d/%d write completed in %.2f seconds",
                            self._log_label,
                            index,
                            total_request_count,
                            loop.time() - request_started,
                        )
                        stage = f"waiting for response {index}/{total_request_count}"
                        self._log_stage("BLE transaction", stage, started, deadline)
                        frame = await self._async_wait_for_connection(
                            future, disconnect_signal, deadline
                        )
                        frames[index - 1] = frame
                        _LOGGER.debug(
                            "%s BLE response %d/%d received %.2f seconds after "
                            "write started (notifications: %d, stale handshakes: %d, "
                            "nonmatching: %d)",
                            self._log_label,
                            index,
                            total_request_count,
                            loop.time() - request_started,
                            notification_count,
                            ignored_handshake_count,
                            nonmatching_notification_count,
                        )
                    if optional_requests:
                        collecting_optional = True
                        future = None
                        optional_futures.extend(
                            loop.create_future() for _request in optional_requests
                        )
                        optional_started = loop.time()
                        optional_deadline = optional_started + optional_timeout
                        for offset, (command, _matcher) in enumerate(
                            optional_requests, start=len(requests) + 1
                        ):
                            initiated_optional_count = offset - len(requests)
                            current_request_index = offset
                            request_started = loop.time()
                            stage = (
                                f"writing optional request {offset}/"
                                f"{total_request_count}"
                            )
                            self._log_stage(
                                "BLE transaction", stage, started, optional_deadline
                            )
                            try:
                                await self._async_wait_for_connection(
                                    client.write_gatt_char(
                                        self._profile.write_char_uuid,
                                        self._encode_application_frame(command),
                                        response=False,
                                    ),
                                    disconnect_signal,
                                    optional_deadline,
                                )
                            except GoveeBleDisconnectedError:
                                discard_connection = True
                                _LOGGER.debug(
                                    "%s optional BLE telemetry stopped after "
                                    "disconnection",
                                    self._log_label,
                                )
                                return tuple(frames)
                            except Exception:
                                discard_connection = True
                                _LOGGER.debug(
                                    "%s optional BLE telemetry write failed; "
                                    "preserving core poll result",
                                    self._log_label,
                                    exc_info=True,
                                )
                                return tuple(frames)
                            _LOGGER.debug(
                                "%s optional BLE request %d/%d write completed in "
                                "%.2f seconds",
                                self._log_label,
                                offset,
                                total_request_count,
                                loop.time() - request_started,
                            )

                        stage = "collecting optional responses"
                        self._log_stage(
                            "BLE transaction", stage, optional_started, optional_deadline
                        )
                        pending = {
                            optional_future
                            for optional_future in optional_futures
                            if not optional_future.done()
                        }
                        if pending:
                            try:
                                await self._async_wait_for_connection(
                                    asyncio.gather(*pending),
                                    disconnect_signal,
                                    optional_deadline,
                                )
                            except GoveeBleDisconnectedError:
                                discard_connection = True
                                _LOGGER.debug(
                                    "%s optional BLE telemetry stopped after "
                                    "disconnection",
                                    self._log_label,
                                )
                            except (TimeoutError, asyncio.TimeoutError):
                                pass
                        received_count = sum(
                            optional_future.done()
                            and not optional_future.cancelled()
                            for optional_future in optional_futures
                        )
                        _LOGGER.debug(
                            "%s optional BLE telemetry received %d/%d responses in "
                            "%.2f seconds",
                            self._log_label,
                            received_count,
                            len(optional_requests),
                            loop.time() - optional_started,
                        )
                        deadline = loop.time() + COMMAND_CONFIRMATION_TIMEOUT
                    return tuple(frames)
                except (TimeoutError, asyncio.TimeoutError) as err:
                    if (
                        stage.startswith("waiting for response")
                        and request_started is not None
                    ):
                        _LOGGER.debug(
                            "%s BLE response timeout diagnostic: request %d/%d, "
                            "%.2f seconds since write started (notifications: %d, "
                            "stale handshakes: %d, nonmatching: %d)",
                            self._log_label,
                            current_request_index,
                            total_request_count,
                            loop.time() - request_started,
                            notification_count,
                            ignored_handshake_count,
                            nonmatching_notification_count,
                        )
                    self._log_timeout("BLE transaction", stage, started)
                    primary_error = GoveeBleClientError(
                        self._transaction_timeout_message(stage)
                    )
                    raise primary_error from err
                except asyncio.CancelledError as err:
                    primary_error = err
                    raise
                except Exception as err:
                    primary_error = err
                    self._log_failure("BLE transaction", stage, started)
                    raise
                except BaseException as err:
                    primary_error = err
                    raise
                finally:
                    if future is not None and not future.done():
                        future.cancel()
                    for optional_future in optional_futures:
                        if not optional_future.done():
                            optional_future.cancel()
                    connection_disconnected = (
                        disconnect_signal is not None and disconnect_signal.is_set()
                    ) or not client.is_connected
                    if connection_disconnected:
                        discard_connection = True
                    if (
                        primary_error is None
                        and not connection_disconnected
                        and not discard_connection
                    ):
                        try:
                            stage = "stopping application notifications"
                            self._log_stage("BLE transaction", stage, started, deadline)
                            await _async_wait_until(
                                client.stop_notify(self._profile.notify_char_uuid),
                                deadline,
                            )
                        except Exception:
                            discard_connection = True
                            _LOGGER.debug(
                                "%s suppressing BLE notification cleanup failure%s",
                                self._log_label,
                                " to preserve primary error" if primary_error else "",
                                exc_info=True,
                            )

            try:
                if self._connection_arbiter is None:
                    result = await self._async_with_connection(operation)
                else:
                    result = await self._async_with_connection_unarbitrated(
                        operation,
                        deadline=loop.time() + transport.CONNECTION_TIMEOUT,
                    )
                if discard_connection:
                    self._cancel_idle_disconnect()
                    await self._async_drop_connection(loop.time() + DISCONNECT_TIMEOUT)
                _LOGGER.debug(
                    "%s BLE transaction completed in %.2f seconds",
                    self._log_label,
                    loop.time() - started,
                )
                return result
            finally:
                self._lock.release()
        finally:
            self._release_connection_lease()

    async def _async_with_connection(
        self,
        operation: Callable[[Any], Any],
        *,
        deadline: float | None = None,
        priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> Any:
        """Run an operation through the shared reusable Bluetooth connection."""

        if self._connection_arbiter is not None:
            return await self._connection_arbiter.async_run(
                self,
                lambda: self._async_with_connection_unarbitrated(
                    operation,
                    deadline=(
                        deadline
                        if deadline is not None
                        else asyncio.get_running_loop().time()
                        + transport.CONNECTION_TIMEOUT
                    ),
                ),
                deadline,
                priority=priority,
            )
        if deadline is None:
            deadline = asyncio.get_running_loop().time() + transport.CONNECTION_TIMEOUT
        return await self._async_with_connection_unarbitrated(
            operation, deadline=deadline
        )

    async def _async_acquire_connection_lease(
        self,
        priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> None:
        """Acquire the integration lease before this client's transaction lock."""

        if self._connection_arbiter is not None:
            await self._connection_arbiter.async_acquire(self, priority=priority)

    def _release_connection_lease(self) -> None:
        """Release a lease acquired before a client transaction lock."""

        if self._connection_arbiter is not None:
            _LOGGER.debug("%s releasing shared BLE connection lease", self._log_label)
            self._connection_arbiter.release()

    async def _async_with_connection_unarbitrated(
        self,
        operation: Callable[[Any], Any],
        *,
        deadline: float,
    ) -> Any:
        """Run an operation while the integration-wide connection lease is held."""

        if self._closed:
            raise GoveeBleClientError("BLE client is closed")
        for future in tuple(self._abandoned_connection_operations):
            if future.done():
                self._abandoned_connection_operations.discard(future)
        if self._abandoned_connection_operations:
            raise GoveeBleClientError("Previous BLE operation is still stopping")

        client = self._client
        if client is None or not client.is_connected:
            started = asyncio.get_running_loop().time()
            connection_revision = self._unexpected_disconnect_revision
            self._log_stage(
                "BLE connection", "establishing transport", started, deadline
            )
            self._clear_connection_state()
            # ``asyncio.wait_for`` inside a transport helper can wait forever for
            # cancellation acknowledgement from a stuck proxy connection attempt.
            # Keep that attempt owned and cancel it without waiting so it cannot
            # monopolize the integration-wide connection lease.
            client = await self._async_wait_for_connection(
                transport.async_establish_connection(
                    self._hass,
                    self._address,
                    self._handle_disconnect,
                    deadline=deadline,
                ),
                None,
                deadline,
            )
            self._client = client
            self._disconnect_signal = asyncio.Event()
            self._connected_at = asyncio.get_running_loop().time()
            self._log_stage("BLE connection", "transport connected", started, deadline)
            if not client.is_connected:
                await self._async_drop_connection(
                    asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
                )
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected while connecting"
                )
            if self._profile.encryption is EncryptionMode.GOVEE_V1:
                handshake_deadline = (
                    asyncio.get_running_loop().time() + HANDSHAKE_TIMEOUT
                )
                try:
                    session_key = await self._async_negotiate_govee_v1_session(
                        client, handshake_deadline
                    )
                    if self._client is not client or not client.is_connected:
                        raise GoveeBleClientError(
                            "Purifier disconnected during encrypted-session setup"
                        )
                    self._session_key = session_key
                    self._session_started_at = asyncio.get_running_loop().time()
                    self._log_stage(
                        "BLE connection",
                        "encrypted session ready",
                        started,
                        handshake_deadline,
                    )
                except (TimeoutError, asyncio.TimeoutError) as err:
                    disconnected = await self._async_drop_after_error(
                        client, connection_revision
                    )
                    if disconnected:
                        raise GoveeBleDisconnectedError(
                            "Purifier disconnected during encrypted-session setup"
                        ) from err
                    raise GoveeBleClientError(
                        "Timed out establishing encrypted purifier session"
                    ) from err
                except GoveeBleClientError as err:
                    disconnected = await self._async_drop_after_error(
                        client, connection_revision
                    )
                    if disconnected and not isinstance(err, GoveeBleDisconnectedError):
                        raise GoveeBleDisconnectedError(
                            "Purifier disconnected during encrypted-session setup"
                        ) from err
                    raise
                except Exception as err:
                    disconnected = await self._async_drop_after_error(
                        client, connection_revision
                    )
                    if disconnected:
                        raise GoveeBleDisconnectedError(
                            "Purifier disconnected during encrypted-session setup"
                        ) from err
                    raise GoveeBleClientError(
                        "Failed to establish encrypted purifier session"
                    ) from err
                except BaseException:
                    await self._async_drop_connection(
                        asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
                    )
                    raise
        else:
            _LOGGER.debug("%s reusing active BLE connection", self._log_label)

        if self._closed:
            await self._async_drop_connection(
                asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT,
                prepare_reconnect=False,
            )
            raise GoveeBleClientError("BLE client is closed")

        operation_revision = self._unexpected_disconnect_revision
        try:
            result = await operation(client)
        except (TimeoutError, asyncio.TimeoutError) as err:
            disconnected = await self._async_drop_after_error(
                client, operation_revision
            )
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                ) from err
            raise GoveeBleClientError("Timed out during BLE transaction") from err
        except Exception as err:
            disconnected = await self._async_drop_after_error(
                client, operation_revision
            )
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                ) from err
            raise
        except BaseException:
            await self._async_drop_connection(
                asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
            )
            raise

        if self._closed:
            await self._async_drop_connection(
                asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT,
                prepare_reconnect=False,
            )
        else:
            self._schedule_idle_disconnect()
        return result

    async def _async_release_for_connection_switch(self, deadline: float) -> None:
        """Release an idle cached connection so another purifier can connect."""

        self._cancel_idle_disconnect()
        task = self._idle_disconnect_task
        if task is not None and task is not asyncio.current_task():
            try:
                await _async_wait_until(asyncio.shield(task), deadline)
            except (TimeoutError, asyncio.TimeoutError) as err:
                raise GoveeBleClientError(
                    "Timed out releasing another purifier's Bluetooth connection"
                ) from err

        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out releasing another purifier's Bluetooth connection"
            ) from err
        try:
            await self._async_drop_connection(deadline)
        finally:
            self._lock.release()

    async def _async_drop_after_error(
        self, client: Any, disconnect_revision: int
    ) -> bool:
        """Drop a failed connection after allowing its callback to run."""

        try:
            if (
                self._unexpected_disconnect_revision != disconnect_revision
                or not client.is_connected
            ):
                return True
            await asyncio.sleep(0)
            return (
                self._unexpected_disconnect_revision != disconnect_revision
                or not client.is_connected
            )
        finally:
            await self._async_drop_connection(
                asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
            )

    def _handle_disconnect(self, client: Any) -> None:
        """Forget only the connection that actually disconnected."""

        if self._client is client:
            now = time.monotonic()
            connection_age = (
                now - self._connected_at if self._connected_at is not None else 0.0
            )
            session_age = (
                now - self._session_started_at
                if self._session_started_at is not None
                else None
            )
            _LOGGER.debug(
                "%s BLE connection disconnected after %.2f seconds%s",
                self._log_label,
                connection_age,
                (
                    f" (encrypted session age: {session_age:.2f} seconds)"
                    if session_age is not None
                    else ""
                ),
            )
            disconnect_signal = self._disconnect_signal
            if disconnect_signal is not None:
                disconnect_signal.set()
            self._clear_connection_state()
            if self._connection_arbiter is not None:
                self._connection_arbiter.connection_released(self)
            self._unexpected_disconnect_revision += 1
            self._mark_connection_stale()

    def _clear_connection_state(self) -> None:
        """Clear all state owned by the current cached connection."""

        self._client = None
        self._disconnect_signal = None
        self._session_key = None
        self._connected_at = None
        self._session_started_at = None

    def _disconnect_signal_for(self, client: Any) -> asyncio.Event | None:
        """Return the disconnect signal only for the exact cached client."""

        if self._client is client:
            return self._disconnect_signal
        return None

    def _cancel_connection_operation(self, future: asyncio.Future[Any]) -> None:
        """Cancel and retain a connection operation until it actually exits."""

        if not future.done() and future not in self._abandoned_connection_operations:
            self._abandoned_connection_operations.add(future)
            future.add_done_callback(self._abandoned_connection_operations.discard)
        _cancel_and_observe(future)

    async def _async_wait_for_connection(
        self,
        awaitable: Awaitable[Any],
        disconnect_signal: asyncio.Event | None,
        deadline: float,
    ) -> Any:
        """Wait for one connection stage or its exact client's disconnect."""

        operation_task = asyncio.ensure_future(awaitable)
        if disconnect_signal is not None and disconnect_signal.is_set():
            self._cancel_connection_operation(operation_task)
            raise GoveeBleDisconnectedError(
                "Purifier disconnected during BLE transaction"
            )
        if operation_task.done():
            return await operation_task

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self._cancel_connection_operation(operation_task)
            raise TimeoutError

        disconnect_task = (
            asyncio.create_task(disconnect_signal.wait())
            if disconnect_signal is not None
            else None
        )
        waiters = (
            (operation_task, disconnect_task)
            if disconnect_task is not None
            else (operation_task,)
        )
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_signal is not None and (
                disconnect_signal.is_set()
                or (disconnect_task is not None and disconnect_task in done)
            ):
                self._cancel_connection_operation(operation_task)
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                )
            if operation_task in done:
                return await operation_task
            self._cancel_connection_operation(operation_task)
            raise TimeoutError
        except asyncio.CancelledError:
            self._cancel_connection_operation(operation_task)
            raise
        finally:
            if disconnect_task is not None:
                _cancel_and_observe(disconnect_task)

    def _mark_connection_stale(self) -> None:
        """Require a post-disconnect advertisement before reconnecting."""

        self._fresh_advertisement_after = time.monotonic()
        if self._hass is not None:
            transport.clear_advertisement_history(self._hass, self._address)

    async def _async_prepare_connection(self) -> None:
        """Prepare an uncached connection before its transaction deadline."""

        if self._closed:
            raise GoveeBleClientError("BLE client is closed")
        client = self._client
        if client is not None and client.is_connected:
            return
        if self._hass is None:
            self._fresh_advertisement_after = None
            return

        loop = asyncio.get_running_loop()
        can_wait = loop.time() >= self._advertisement_retry_at
        try:
            await transport.async_prepare_connection_path(
                self._hass,
                self._address,
                after=self._fresh_advertisement_after,
                wait_for_advertisement=can_wait,
            )
        except GoveeBleClientError:
            if can_wait:
                retry_delay = ADVERTISEMENT_RETRY_DELAYS[
                    min(
                        self._advertisement_failure_count,
                        len(ADVERTISEMENT_RETRY_DELAYS) - 1,
                    )
                ]
                self._advertisement_failure_count += 1
                self._advertisement_retry_at = loop.time() + retry_delay
                _LOGGER.debug(
                    "%s fresh-advertisement recovery will retry in %.0f seconds",
                    self._log_label,
                    retry_delay,
                )
            raise

        self._fresh_advertisement_after = None
        self._advertisement_failure_count = 0
        self._advertisement_retry_at = 0.0

    def _encode_application_frame(self, frame: bytes) -> bytes:
        """Encode one plaintext profile frame for the active transport."""

        if self._profile.encryption is EncryptionMode.NONE:
            return frame
        if self._session_key is None:
            raise GoveeBleClientError("Encrypted purifier session is unavailable")
        return encrypt_frame(frame, self._session_key)

    def _decode_application_frame(self, frame: bytes) -> bytes:
        """Decode one wire notification into a plaintext protocol frame."""

        if self._profile.encryption is EncryptionMode.NONE:
            return frame
        if self._session_key is None:
            raise ProtocolError("Encrypted purifier session is unavailable")
        return decrypt_frame(frame, self._session_key)

    async def _async_negotiate_govee_v1_session(
        self, client: Any, deadline: float
    ) -> bytes:
        """Negotiate one connection-specific Govee V1 session key."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        future: asyncio.Future[bytes] | None = None
        expected_command = 0
        primary_error: BaseException | None = None
        stage = "starting handshake notifications"
        disconnect_signal = self._disconnect_signal_for(client)

        def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
            if future is None or future.done():
                return
            try:
                frame = decrypt_frame(bytes(data), COMMUNICATION_KEY)
            except ProtocolError as err:
                future.set_exception(err)
                return
            if frame[:2] == bytes((0xE7, expected_command)):
                future.set_result(frame)

        try:
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            await self._async_wait_for_connection(
                client.start_notify(
                    self._profile.notify_char_uuid, notification_handler
                ),
                disconnect_signal,
                deadline,
            )

            expected_command = 0x01
            session_request = build_handshake_request(expected_command)
            future = loop.create_future()
            stage = "writing e7 01 request"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            await self._async_wait_for_connection(
                client.write_gatt_char(
                    self._profile.write_char_uuid,
                    encrypt_frame(session_request, COMMUNICATION_KEY),
                    response=False,
                ),
                disconnect_signal,
                deadline,
            )
            stage = "waiting for e7 01 response"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            session_response = await self._async_wait_for_connection(
                future, disconnect_signal, deadline
            )
            stage = "validating e7 01 response"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            session_key = parse_session_key(session_response)

            expected_command = 0x02
            confirmation_request = build_handshake_request(expected_command)
            future = loop.create_future()
            stage = "writing e7 02 request"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            await self._async_wait_for_connection(
                client.write_gatt_char(
                    self._profile.write_char_uuid,
                    encrypt_frame(confirmation_request, COMMUNICATION_KEY),
                    response=False,
                ),
                disconnect_signal,
                deadline,
            )
            stage = "waiting for e7 02 response"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            confirmation_response = await self._async_wait_for_connection(
                future, disconnect_signal, deadline
            )
            stage = "validating e7 02 response"
            self._log_stage("Govee V1 handshake", stage, started, deadline)
            validate_handshake_confirmation(confirmation_response, confirmation_request)
            _LOGGER.debug(
                "%s Govee V1 handshake completed in %.2f seconds",
                self._log_label,
                loop.time() - started,
            )
            return session_key
        except (TimeoutError, asyncio.TimeoutError) as err:
            primary_error = err
            self._log_timeout("Govee V1 handshake", stage, started)
            raise
        except asyncio.CancelledError as err:
            primary_error = err
            raise
        except Exception as err:
            primary_error = err
            self._log_failure("Govee V1 handshake", stage, started)
            raise
        except BaseException as err:
            primary_error = err
            raise
        finally:
            if future is not None and not future.done():
                future.cancel()
            connection_disconnected = (
                disconnect_signal is not None and disconnect_signal.is_set()
            ) or not client.is_connected
            if primary_error is None and not connection_disconnected:
                try:
                    self._log_stage(
                        "Govee V1 handshake",
                        "stopping handshake notifications",
                        started,
                        deadline,
                    )
                    await _async_wait_until(
                        client.stop_notify(self._profile.notify_char_uuid), deadline
                    )
                except Exception:
                    _LOGGER.debug(
                        "%s suppressing encrypted-session notification cleanup "
                        "failure%s",
                        self._log_label,
                        " to preserve primary error" if primary_error else "",
                        exc_info=True,
                    )
                    if primary_error is None:
                        raise

    @staticmethod
    def _transaction_timeout_message(stage: str) -> str:
        """Describe the timed-out transaction stage without implying a write."""

        if stage.startswith("waiting for response"):
            return "Timed out waiting for purifier response"
        if stage.startswith("writing request"):
            return "Timed out writing purifier request"
        if stage == "starting application notifications":
            return "Timed out starting purifier notifications"
        return "Timed out during purifier transaction"

    def _log_stage(
        self, operation: str, stage: str, started: float, deadline: float
    ) -> None:
        """Log one BLE operation stage without device or protocol secrets."""

        now = asyncio.get_running_loop().time()
        _LOGGER.debug(
            "%s %s stage: %s (%.2f seconds elapsed, %.2f seconds remaining)",
            self._log_label,
            operation,
            stage,
            now - started,
            max(0.0, deadline - now),
        )

    def _log_timeout(self, operation: str, stage: str, started: float) -> None:
        """Log the precise stage that exhausted an operation deadline."""

        elapsed = asyncio.get_running_loop().time() - started
        _LOGGER.debug(
            "%s %s timed out during %s after %.2f seconds",
            self._log_label,
            operation,
            stage,
            elapsed,
            exc_info=True,
        )

    def _log_failure(self, operation: str, stage: str, started: float) -> None:
        """Log the precise stage that failed an operation."""

        elapsed = asyncio.get_running_loop().time() - started
        _LOGGER.debug(
            "%s %s failed during %s after %.2f seconds",
            self._log_label,
            operation,
            stage,
            elapsed,
            exc_info=True,
        )

    def _cancel_idle_disconnect(self) -> None:
        """Cancel an idle timer that has not started disconnecting."""

        if self._idle_disconnect_handle is not None:
            self._idle_disconnect_handle.cancel()
            self._idle_disconnect_handle = None

    async def _async_wait_for_idle_disconnect(self) -> None:
        """Wait for an idle disconnect that already won the scheduling race."""

        task = self._idle_disconnect_task
        if task is None or task is asyncio.current_task():
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + DISCONNECT_TIMEOUT
        self._log_stage(
            "BLE preflight", "waiting for idle disconnect cleanup", started, deadline
        )
        try:
            await _async_wait_until(asyncio.shield(task), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._log_timeout(
                "BLE preflight", "waiting for idle disconnect cleanup", started
            )
            raise GoveeBleClientError(
                "Timed out waiting for idle disconnect cleanup"
            ) from err

    def _schedule_idle_disconnect(self) -> None:
        """Release a healthy connection after an idle period."""

        self._cancel_idle_disconnect()
        if self._closed:
            return
        _LOGGER.debug(
            "%s BLE idle disconnect scheduled in %.2f seconds",
            self._log_label,
            self._connection_idle_timeout,
        )
        loop = asyncio.get_running_loop()
        self._idle_disconnect_handle = loop.call_later(
            self._connection_idle_timeout, self._start_idle_disconnect
        )

    def _start_idle_disconnect(self) -> None:
        """Start serialized idle cleanup when its timer expires."""

        self._idle_disconnect_handle = None
        if self._closed or self._idle_disconnect_task is not None:
            return
        _LOGGER.debug("%s BLE idle timeout reached", self._log_label)

        async def disconnect_idle_client() -> None:
            try:
                async with self._lock:
                    deadline = asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
                    await self._async_drop_connection(deadline)
            finally:
                if self._idle_disconnect_task is asyncio.current_task():
                    self._idle_disconnect_task = None

        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            task = self._hass.async_create_task(disconnect_idle_client())
        else:
            task = asyncio.create_task(disconnect_idle_client())
        self._idle_disconnect_task = task

    async def _async_drop_connection(
        self, deadline: float, *, prepare_reconnect: bool = True
    ) -> None:
        """Forget and best-effort disconnect the cached connection."""

        client = self._client
        now = time.monotonic()
        connection_age = (
            now - self._connected_at if self._connected_at is not None else None
        )
        session_age = (
            now - self._session_started_at
            if self._session_started_at is not None
            else None
        )
        self._clear_connection_state()
        if self._connection_arbiter is not None:
            self._connection_arbiter.connection_released(self)
        if client is not None:
            _LOGGER.debug(
                "%s releasing cached BLE connection%s%s",
                self._log_label,
                (
                    f" after {connection_age:.2f} seconds"
                    if connection_age is not None
                    else ""
                ),
                (
                    f" (encrypted session age: {session_age:.2f} seconds)"
                    if session_age is not None
                    else ""
                ),
            )
            await transport.async_disconnect(client, deadline=deadline)
            if prepare_reconnect:
                self._mark_connection_stale()

    async def async_close(self) -> None:
        """Cancel idle cleanup and close the cached connection."""

        _LOGGER.debug(
            "%s BLE client closing (cached connection: %s)",
            self._log_label,
            self._client is not None,
        )
        self._closed = True
        self._cancel_idle_disconnect()
        task = self._idle_disconnect_task
        if task is not None and task is not asyncio.current_task():
            await asyncio.gather(task, return_exceptions=True)

        deadline = (
            asyncio.get_running_loop().time() + DEFAULT_TIMEOUT + DISCONNECT_TIMEOUT
        )
        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError):
            _LOGGER.debug(
                "%s timed out waiting to close BLE client",
                self._log_label,
                exc_info=True,
            )
            return
        try:
            await self._async_drop_connection(deadline, prepare_reconnect=False)
        finally:
            self._lock.release()
        _LOGGER.debug("%s BLE client closed", self._log_label)
