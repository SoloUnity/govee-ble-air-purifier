"""BLE client for H712-family Govee air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
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


class GoveeBleClient:
    """Small serialized request/response BLE client."""

    def __init__(
        self,
        hass: Any,
        address: str,
        *,
        profile: ModelProfile = H7124_PROFILE,
        polling_interval_seconds: float | None = None,
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
                requests: list[tuple[bytes, Callable[[bytes], bool]]] = [
                    (
                        self._profile.state_query_command,
                        self._profile.is_power_state_response,
                    ),
                    (
                        self._profile.status_query_command,
                        self._profile.is_status_response,
                    ),
                ]
                if (night_light := self._profile.night_light) is not None:
                    requests.extend(
                        (
                            (
                                night_light.power_brightness_query_command,
                                is_night_light_power_brightness_response,
                            ),
                            (
                                night_light.rgb_state_query_command,
                                is_night_light_rgb_state_response,
                            ),
                        )
                    )
                frames = await self._async_write_and_wait_many(
                    tuple(requests),
                    timeout=POLL_TIMEOUT,
                )
                power_frame, status_frame = frames[:2]
                status = self._profile.decode_status(status_frame)
                night_light_state = None
                if night_light is not None:
                    power_brightness = decode_night_light_power_brightness(frames[2])
                    night_light_state = NightLightState(
                        is_on=power_brightness.is_on,
                        brightness_percent=power_brightness.brightness_percent,
                        rgb_color=decode_night_light_rgb_state(frames[3]),
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
        await self._async_prepare_connection()
        deadline = loop.time() + DEFAULT_TIMEOUT
        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for BLE transaction lock"
            ) from err
        try:
            await self._async_with_connection(operation)
        finally:
            self._lock.release()

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
        return frames[0]

    async def _async_write_and_wait_many(
        self,
        requests: tuple[tuple[bytes, Callable[[bytes], bool]], ...],
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> tuple[bytes, ...]:
        loop = asyncio.get_running_loop()
        stage = "waiting for transaction lock"
        _LOGGER.debug(
            "%s BLE transaction started with %d requests (%.2f second timeout)",
            self._log_label,
            len(requests),
            timeout,
        )
        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect()
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
        try:
            frames: list[bytes] = []
            future: asyncio.Future[bytes] | None = None
            discard_connection = False
            current_request_index = 0
            request_started: float | None = None
            notification_count = 0
            ignored_handshake_count = 0
            nonmatching_notification_count = 0

            def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
                nonlocal future
                nonlocal ignored_handshake_count
                nonlocal nonmatching_notification_count
                nonlocal notification_count
                if future is None or len(frames) >= len(requests):
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
                    if not future.done():
                        future.set_exception(err)
                    return
                matcher = requests[len(frames)][1]
                if not matcher(frame):
                    nonmatching_notification_count += 1
                    return
                try:
                    validate_frame(frame)
                except ProtocolError as err:
                    if not future.done():
                        future.set_exception(err)
                    return
                if not future.done():
                    future.set_result(frame)

            async def operation(client: Any) -> tuple[bytes, ...]:
                nonlocal current_request_index
                nonlocal deadline, discard_connection, future
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
                        stage = f"writing request {index}/{len(requests)}"
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
                            len(requests),
                            loop.time() - request_started,
                        )
                        stage = f"waiting for response {index}/{len(requests)}"
                        self._log_stage("BLE transaction", stage, started, deadline)
                        frame = await self._async_wait_for_connection(
                            future, disconnect_signal, deadline
                        )
                        frames.append(frame)
                        _LOGGER.debug(
                            "%s BLE response %d/%d received %.2f seconds after "
                            "write started (notifications: %d, stale handshakes: %d, "
                            "nonmatching: %d)",
                            self._log_label,
                            index,
                            len(requests),
                            loop.time() - request_started,
                            notification_count,
                            ignored_handshake_count,
                            nonmatching_notification_count,
                        )
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
                            len(requests),
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
                    connection_disconnected = (
                        disconnect_signal is not None and disconnect_signal.is_set()
                    ) or not client.is_connected
                    if primary_error is None and not connection_disconnected:
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

            result = await self._async_with_connection(operation)
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

    async def _async_with_connection(
        self,
        operation: Callable[[Any], Any],
        *,
        deadline: float | None = None,
    ) -> Any:
        """Run an operation through the shared reusable Bluetooth connection."""

        if deadline is None:
            deadline = asyncio.get_running_loop().time() + transport.CONNECTION_TIMEOUT
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
            client = await transport.async_establish_connection(
                self._hass,
                self._address,
                self._handle_disconnect,
                deadline=deadline,
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
