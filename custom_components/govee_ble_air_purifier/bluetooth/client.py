"""BLE client for H712-family Govee air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
import logging
import time
from typing import Any

from ..govee_ble_air_purifier_protocol import (
    EncryptionMode,
    H7124_PROFILE,
    ModelProfile,
    NightLightState,
    NightLightPollingRequestOrder,
    NightLightProfile,
    ProtocolError,
    PurifierPushUpdate,
    PurifierState,
    COMMUNICATION_KEY,
    build_handshake_request,
    decode_night_light_power_brightness,
    decode_night_light_rgb_state,
    decrypt_frame,
    encrypt_frame,
    identify_handshake_frame,
    is_command_echo,
    is_fan_mode_confirmation,
    is_night_light_brightness_confirmation,
    is_night_light_power_brightness_response,
    is_night_light_power_confirmation,
    is_night_light_rgb_state_response,
    is_power_confirmation,
    parse_session_key,
    validate_frame,
    validate_handshake_confirmation,
)
from . import GoveeBleClientError, GoveeBleDisconnectedError, transport
from . import _arbiter
from ._arbiter import ConnectionLeasePriority as _ConnectionLeasePriority
from ._connection import ConnectionDependencies, ConnectionManager
from ._night_light_polling import NightLightPollingTracker
from ._push import PushDispatcher
from ._session import GoveeBleSession
from ._transactions import (
    ConnectedTransactionSession,
    ExchangePlan,
    ExchangeRequest,
    ExchangeResult,
    TransactionRunner,
)
from .transport import _async_wait_until

DEFAULT_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0
COMMAND_CONFIRMATION_TIMEOUT = 2.0
HANDSHAKE_TIMEOUT = 10.0
CONNECTION_IDLE_GRACE = _arbiter.CONNECTION_IDLE_GRACE
MAX_CONNECTION_IDLE_TIMEOUT = _arbiter.MAX_CONNECTION_IDLE_TIMEOUT
DISCONNECT_TIMEOUT = _arbiter.DISCONNECT_TIMEOUT
CONNECTION_LEASE_TIMEOUT = _arbiter.CONNECTION_LEASE_TIMEOUT
ADVERTISEMENT_RETRY_DELAYS = (60.0, 120.0, 300.0)
_LOGGER = logging.getLogger(__name__)
_ABANDONED_OPERATION_FUTURES: set[asyncio.Future[Any]] = set()


def connection_idle_timeout_for_polling_interval(
    polling_interval_seconds: float,
) -> float:
    """Retain through the next poll or release after a short activity grace."""

    next_poll_timeout = polling_interval_seconds + CONNECTION_IDLE_GRACE
    if next_poll_timeout <= MAX_CONNECTION_IDLE_TIMEOUT:
        return next_poll_timeout
    return CONNECTION_IDLE_GRACE


class GoveeConnectionArbiter(_arbiter.GoveeConnectionArbiter):
    """Compatibility facade for the extracted connection scheduler."""

    def __init__(self) -> None:
        super().__init__()
        self._lease_timeout = lambda: CONNECTION_LEASE_TIMEOUT


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
        self._session = GoveeBleSession(_cancel_and_observe)
        self._idle_disconnect_handle: asyncio.TimerHandle | None = None
        self._idle_disconnect_task: asyncio.Task[Any] | None = None
        self._fresh_advertisement_after: float | None = None
        self._advertisement_retry_at = 0.0
        self._advertisement_failure_count = 0
        self._push_dispatcher = PushDispatcher(profile, self._log_label)
        self._monotonic: Callable[[], float] = time.monotonic
        self._night_light_polling = NightLightPollingTracker(profile.night_light)
        self._closed = False
        self._connection = ConnectionManager(
            self._session,
            self._lock,
            ConnectionDependencies(
                establish=lambda _callback, deadline: (
                    transport.async_establish_connection(
                        self._hass,
                        self._address,
                        self._handle_disconnect,
                        deadline=deadline,
                    )
                ),
                negotiate_session=lambda client, deadline: (
                    self._async_negotiate_govee_v1_session(client, deadline)
                ),
                wait_for_connection=lambda awaitable, signal, deadline: (
                    self._async_wait_for_connection(awaitable, signal, deadline)
                ),
                disconnect=lambda client, deadline: transport.async_disconnect(
                    client, deadline=deadline
                ),
                acquire_recovery_lease=self._async_acquire_connection_lease,
                release_recovery_lease=self._release_connection_lease,
                connection_released=self._connection_released,
                mark_connection_stale=self._mark_connection_stale,
                schedule_idle_disconnect=self._schedule_idle_disconnect,
                create_task=self._create_task,
                is_closed=lambda: self._closed,
                encryption_mode=lambda: self._profile.encryption,
                notify_char_uuid=lambda: self._profile.notify_char_uuid,
                handshake_timeout=lambda: HANDSHAKE_TIMEOUT,
                disconnect_timeout=lambda: DISCONNECT_TIMEOUT,
                monotonic=lambda: self._monotonic(),
                log_stage=self._log_stage,
            ),
            log_label=self._log_label,
            logger=_LOGGER,
        )

    # Compatibility properties preserve the integration's long-standing private
    # monkeypatch seams while runtime code delegates connection state to
    # ``GoveeBleSession`` through its narrow methods.
    @property
    def _client(self) -> Any | None:
        return self._session.client

    @_client.setter
    def _client(self, client: Any | None) -> None:
        self._session.compat_set_client(client)

    @property
    def _disconnect_signal(self) -> asyncio.Event | None:
        return self._session.disconnect_signal

    @_disconnect_signal.setter
    def _disconnect_signal(self, signal: asyncio.Event | None) -> None:
        self._session.compat_set_disconnect_signal(signal)

    @property
    def _session_key(self) -> bytes | None:
        return self._session.session_key

    @_session_key.setter
    def _session_key(self, session_key: bytes | None) -> None:
        self._session.compat_set_session_key(session_key)

    @property
    def _connected_at(self) -> float | None:
        return self._session.connected_at

    @_connected_at.setter
    def _connected_at(self, connected_at: float | None) -> None:
        self._session.compat_set_connected_at(connected_at)

    @property
    def _session_started_at(self) -> float | None:
        return self._session.session_started_at

    @_session_started_at.setter
    def _session_started_at(self, started_at: float | None) -> None:
        self._session.compat_set_session_started_at(started_at)

    @property
    def _application_notifications_client(self) -> Any | None:
        return self._session.notifications_client

    @_application_notifications_client.setter
    def _application_notifications_client(self, client: Any | None) -> None:
        self._session.compat_set_notifications_client(client)

    @property
    def _connection_generation(self) -> int:
        return self._session.generation

    @_connection_generation.setter
    def _connection_generation(self, generation: int) -> None:
        self._session.compat_set_generation(generation)

    @property
    def _unexpected_disconnect_revision(self) -> int:
        return self._session.unexpected_disconnect_revision

    @_unexpected_disconnect_revision.setter
    def _unexpected_disconnect_revision(self, revision: int) -> None:
        self._session.compat_set_unexpected_disconnect_revision(revision)

    @property
    def _abandoned_connection_operations(self) -> set[asyncio.Future[Any]]:
        return self._session.abandoned_operations

    @property
    def _notification_recovery_task(self) -> asyncio.Task[Any] | None:
        return self._connection.notification_recovery_task

    @_notification_recovery_task.setter
    def _notification_recovery_task(self, task: asyncio.Task[Any] | None) -> None:
        self._connection.compat_set_notification_recovery_task(task)

    @property
    def _notification_recovery_started_at(self) -> float | None:
        return self._connection.notification_recovery_started_at

    @_notification_recovery_started_at.setter
    def _notification_recovery_started_at(self, started_at: float | None) -> None:
        self._connection.compat_set_notification_recovery_started_at(started_at)

    @property
    def _persistent_notifications_enabled(self) -> bool:
        """Return whether this model keeps application notifications active."""

        push = self._profile.push_notifications
        return push is not None and push.enabled

    def set_push_callback(
        self, callback: Callable[[PurifierPushUpdate], None] | None
    ) -> None:
        """Register or detach the coordinator's non-blocking push callback."""

        self._push_dispatcher.set_callback(callback)

    def diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive persistent-notification diagnostics."""

        self._sync_night_light_polling_profile()
        now = self._monotonic()
        notification_recovery_active = self._connection.notification_recovery_active
        diagnostics = {
            "persistent_notifications_enabled": self._persistent_notifications_enabled,
            "notifications_active": (
                self._session.client is not None
                and self._session.notifications_active_for(self._session.client)
            ),
            "connection_generation": self._session.generation,
            "quarantined_operation_count": self._session.quarantined_operation_count,
            "notification_recovery_active": notification_recovery_active,
            "notification_recovery_age_seconds": (
                self._connection.notification_recovery_age(now)
            ),
            "night_light_polling": self._night_light_polling.diagnostics(now),
        }
        diagnostics.update(self._push_dispatcher.diagnostics())
        return diagnostics

    def _claim_night_light_poll(self, now: float) -> bool:
        """Reserve one due light reconciliation before any transaction await."""

        self._sync_night_light_polling_profile()
        return self._night_light_polling.claim(now)

    def _sync_night_light_polling_profile(self) -> None:
        """Keep the tracker aligned if a test or caller replaces the profile."""

        if self._night_light_polling.profile is not self._profile.night_light:
            self._night_light_polling = NightLightPollingTracker(
                self._profile.night_light
            )

    def _release_night_light_poll_claim(self) -> None:
        """Make a periodic reconciliation due again when core polling failed."""

        self._night_light_polling.release_claim()

    def _record_night_light_poll_result(
        self, frames: tuple[bytes | None, bytes | None]
    ) -> None:
        """Record reconciliation health and schedule profile-defined backoff."""

        self._night_light_polling.record_result(frames, self._monotonic())

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
                poll_night_light = self._claim_night_light_poll(self._monotonic())
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
                try:
                    frames = await self._async_write_and_wait_many(
                        requests,
                        timeout=POLL_TIMEOUT,
                        optional_requests=optional_requests,
                        optional_timeout=(
                            night_light.polling.timeout_seconds
                            if night_light is not None
                            else 0.0
                        ),
                        optional_request_order=(
                            night_light.polling.request_order
                            if night_light is not None
                            else NightLightPollingRequestOrder.PIPELINED
                        ),
                        lease_priority=_ConnectionLeasePriority.POLL,
                    )
                except BaseException:
                    if poll_night_light:
                        self._release_night_light_poll_claim()
                    raise
                power_frame, status_frame = frames[:2]
                assert power_frame is not None and status_frame is not None
                status = self._profile.decode_status(status_frame)
                night_light_state = None
                if poll_night_light:
                    power_brightness_frame, rgb_frame = frames[2:]
                    self._record_night_light_poll_result(
                        (power_brightness_frame, rgb_frame)
                    )
                    if power_brightness_frame is not None or rgb_frame is not None:
                        power_brightness = (
                            decode_night_light_power_brightness(power_brightness_frame)
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

    def _application_notification_handler(
        self, client: Any, generation: int
    ) -> Callable[[Any, bytearray | bytes], None]:
        """Build a callback bound to one exact cached connection generation."""

        def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
            if self._closed or not self._session.is_current(client, generation):
                return
            route = self._session.transaction_route
            session_key_available = self._session.session_key is not None
            try:
                frame = self._decode_application_frame(bytes(data))
                validate_frame(frame)
            except ProtocolError as err:
                if (
                    self._profile.encryption is EncryptionMode.GOVEE_V1
                    and session_key_available
                ):
                    handshake_command = identify_handshake_frame(bytes(data))
                    if handshake_command is not None:
                        if route is not None:
                            route.handle_stale_handshake(handshake_command)
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
                if route is not None:
                    route.handle_error(err)
                else:
                    self._schedule_notification_recovery(client, generation)
                return

            if route is not None and route.handle_frame(frame):
                return
            if self._dispatch_push_frame(frame):
                return
            if route is not None:
                route.handle_nonmatching()

        return notification_handler

    def _schedule_notification_recovery(self, client: Any, generation: int) -> None:
        """Invalidate a malformed idle listener and release its connection."""

        self._connection.schedule_notification_recovery(client, generation)

    def _dispatch_push_frame(self, frame: bytes) -> bool:
        """Decode and publish one profile-enabled unsolicited frame."""

        return self._push_dispatcher.dispatch(frame)

    async def _async_ensure_application_notifications(
        self, client: Any, deadline: float
    ) -> None:
        """Start one application listener for the current connection."""

        if self._session.notifications_active_for(client):
            return
        generation = self._session.generation
        disconnect_signal = self._session.disconnect_signal_for(client)
        await self._async_wait_for_connection(
            client.start_notify(
                self._profile.notify_char_uuid,
                self._application_notification_handler(client, generation),
            ),
            disconnect_signal,
            deadline,
        )
        self._session.mark_notifications_active(client, generation)
        _LOGGER.debug(
            "%s application notifications active for connection generation %d",
            self._log_label,
            generation,
        )

    async def _async_stop_application_notifications(
        self, client: Any, deadline: float
    ) -> None:
        """Invalidate and best-effort stop one application listener."""

        if not self._session.release_notifications(client):
            return
        await self._async_wait_for_connection(
            client.stop_notify(self._profile.notify_char_uuid), None, deadline
        )

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
                if self._persistent_notifications_enabled:
                    await self._async_ensure_application_notifications(client, deadline)
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
        optional_requests: tuple[tuple[bytes, Callable[[bytes], bool]], ...] = (),
        optional_timeout: float = 0.0,
        optional_request_order: NightLightPollingRequestOrder = (
            NightLightPollingRequestOrder.PIPELINED
        ),
        lease_priority: _ConnectionLeasePriority = _ConnectionLeasePriority.COMMAND,
    ) -> tuple[bytes | None, ...]:
        loop = asyncio.get_running_loop()
        plan = ExchangePlan(
            required=tuple(ExchangeRequest(*request) for request in requests),
            optional=tuple(ExchangeRequest(*request) for request in optional_requests),
            timeout=timeout,
            optional_timeout=optional_timeout,
            optional_order=optional_request_order,
            cleanup_timeout=COMMAND_CONFIRMATION_TIMEOUT,
        )
        _LOGGER.debug(
            "%s BLE transaction started with %d requests (%.2f second timeout)",
            self._log_label,
            plan.request_count,
            timeout,
        )
        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect()
        await self._async_acquire_connection_lease(lease_priority)
        try:
            await self._async_prepare_connection()
            started = loop.time()
            deadline = started + timeout
            self._log_stage(
                "BLE transaction", "waiting for transaction lock", started, deadline
            )
            try:
                await _async_wait_until(self._lock.acquire(), deadline)
            except (TimeoutError, asyncio.TimeoutError) as err:
                self._log_timeout(
                    "BLE transaction", "waiting for transaction lock", started
                )
                raise GoveeBleClientError(
                    "Timed out waiting for BLE transaction lock"
                ) from err
            async def operation(client: Any) -> ExchangeResult:
                transaction_session = ConnectedTransactionSession(
                    session=self._session,
                    client=client,
                    write_char_uuid=self._profile.write_char_uuid,
                    encryption=self._profile.encryption,
                    persistent_notifications_enabled=(
                        self._persistent_notifications_enabled
                    ),
                    ensure_notifications=self._async_ensure_application_notifications,
                    stop_notifications=self._async_stop_application_notifications,
                )
                runner = TransactionRunner(
                    log_label=self._log_label,
                    debug=_LOGGER.debug,
                    log_stage=self._log_stage,
                    log_timeout=self._log_timeout,
                    log_failure=self._log_failure,
                    timeout_message=self._transaction_timeout_message,
                )
                return await runner.async_exchange(transaction_session, plan)

            try:
                if self._connection_arbiter is None:
                    exchange = await self._async_with_connection(operation)
                else:
                    exchange = await self._async_with_connection_unarbitrated(
                        operation,
                        deadline=loop.time() + transport.CONNECTION_TIMEOUT,
                    )
                if exchange.discard_session:
                    self._cancel_idle_disconnect()
                    await self._async_drop_connection(loop.time() + DISCONNECT_TIMEOUT)
                _LOGGER.debug(
                    "%s BLE transaction completed in %.2f seconds",
                    self._log_label,
                    loop.time() - started,
                )
                return exchange.frames
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

    def _connection_released(self) -> None:
        """Notify the optional shared-slot scheduler after detaching transport."""

        if self._connection_arbiter is not None:
            self._connection_arbiter.connection_released(self)

    def _create_task(
        self, coroutine: Coroutine[Any, Any, Any]
    ) -> asyncio.Task[Any]:
        """Create manager-owned work through Home Assistant when available."""

        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            return self._hass.async_create_task(coroutine)
        return asyncio.create_task(coroutine)

    async def _async_with_connection_unarbitrated(
        self,
        operation: Callable[[Any], Any],
        *,
        deadline: float,
    ) -> Any:
        """Run an operation while the integration-wide connection lease is held."""

        return await self._connection.async_run(operation, deadline=deadline)

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

        return await self._connection.async_drop_after_error(
            client, disconnect_revision
        )

    def _handle_disconnect(self, client: Any) -> None:
        """Forget only the connection that actually disconnected."""

        self._connection.handle_disconnect(client)

    def _clear_connection_state(self) -> None:
        """Clear all state owned by the current cached connection."""

        self._session.reset_for_connection_attempt()

    def _disconnect_signal_for(self, client: Any) -> asyncio.Event | None:
        """Return the disconnect signal only for the exact cached client."""

        return self._session.disconnect_signal_for(client)

    def _cancel_connection_operation(self, future: asyncio.Future[Any]) -> None:
        """Cancel and retain a connection operation until it actually exits."""

        self._session.cancel_operation(future)
        if not future.done():
            _LOGGER.debug(
                "%s quarantined a BLE operation that has not acknowledged cancellation",
                self._log_label,
            )

    async def _async_wait_for_connection(
        self,
        awaitable: Awaitable[Any],
        disconnect_signal: asyncio.Event | None,
        deadline: float,
    ) -> Any:
        """Wait for one connection stage or its exact client's disconnect."""

        return await self._session.async_wait(awaitable, disconnect_signal, deadline)

    def _mark_connection_stale(self) -> None:
        """Require a post-disconnect advertisement before reconnecting."""

        self._fresh_advertisement_after = time.monotonic()
        if self._hass is not None:
            transport.clear_advertisement_history(self._hass, self._address)

    async def _async_prepare_connection(self) -> None:
        """Prepare an uncached connection before its transaction deadline."""

        if self._closed:
            raise GoveeBleClientError("BLE client is closed")
        if self._session.has_connected_client():
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
        return self._session.encode(frame, self._profile.encryption)

    def _decode_application_frame(self, frame: bytes) -> bytes:
        """Decode one wire notification into a plaintext protocol frame."""

        if self._profile.encryption is EncryptionMode.NONE:
            return frame
        return self._session.decode(frame, self._profile.encryption)

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
        if self._persistent_notifications_enabled and self._connection_arbiter is None:
            _LOGGER.debug(
                "%s retaining dedicated BLE connection for push notifications",
                self._log_label,
            )
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

        await self._connection.async_drop(
            deadline, prepare_reconnect=prepare_reconnect
        )

    async def async_close(self) -> None:
        """Cancel idle cleanup and close the cached connection."""

        _LOGGER.debug(
            "%s BLE client closing (cached connection: %s)",
            self._log_label,
            self._session.client is not None,
        )
        self._closed = True
        self._cancel_idle_disconnect()
        await self._connection.async_cancel_notification_recovery()
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
