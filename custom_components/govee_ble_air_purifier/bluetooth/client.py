"""BLE client for H712-family Govee air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from ..const import DEFAULT_POLLING_INTERVAL_SECONDS
from ..models import PurifierState
from ..profiles import EncryptionMode, H7124_PROFILE, ModelProfile
from ..protocol import (
    is_fan_mode_confirmation,
    is_power_confirmation,
)
from . import GoveeBleClientError, transport
from .framing import ProtocolError, validate_frame
from .govee_v1 import (
    COMMUNICATION_KEY,
    build_handshake_request,
    decrypt_frame,
    encrypt_frame,
    parse_session_key,
    validate_handshake_confirmation,
)
from .transport import _async_wait_until

DEFAULT_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0
COMMAND_CONFIRMATION_TIMEOUT = 2.0
CONNECTION_IDLE_GRACE = 5.0
MAX_CONNECTION_IDLE_TIMEOUT = 30.0
DISCONNECT_TIMEOUT = 5.0
_LOGGER = logging.getLogger(__name__)


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
        polling_interval_seconds: float = DEFAULT_POLLING_INTERVAL_SECONDS,
    ) -> None:
        self._hass = hass
        self._address = address
        self._profile = profile
        self._connection_idle_timeout = (
            connection_idle_timeout_for_polling_interval(polling_interval_seconds)
        )
        self._lock = asyncio.Lock()
        self._client: Any = None
        self._session_key: bytes | None = None
        self._idle_disconnect_handle: asyncio.TimerHandle | None = None
        self._idle_disconnect_task: asyncio.Task[Any] | None = None
        self._closed = False

    async def async_get_state(self) -> PurifierState:
        """Poll power, PM2.5, and filter-life state."""

        power_frame, status_frame = await self._async_write_and_wait_many(
            (
                (self._profile.state_query_command, self._profile.is_power_state_response),
                (self._profile.status_query_command, self._profile.is_status_response),
            ),
            timeout=POLL_TIMEOUT,
        )
        status = self._profile.decode_status(status_frame)
        return PurifierState(
            is_on=self._profile.decode_power_state(power_frame),
            pm25=status.pm25,
            filter_life=status.filter_life,
        )

    async def async_set_power(self, is_on: bool) -> bool:
        """Set purifier power."""

        command = (
            self._profile.power_on_command
            if is_on
            else self._profile.power_off_command
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

    async def _async_write_without_response(self, command: bytes) -> None:
        await self._async_write_commands_without_response((command,))

    async def _async_write_commands_without_response(
        self, commands: tuple[bytes, ...]
    ) -> None:
        deadline = asyncio.get_running_loop().time() + DEFAULT_TIMEOUT

        async def operation(client: Any) -> None:
            for command in commands:
                await _async_wait_until(
                    client.write_gatt_char(
                        self._profile.write_char_uuid,
                        self._encode_application_frame(command),
                        response=False,
                    ),
                    deadline,
                )

        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect(deadline)
        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for purifier response"
            ) from err
        try:
            await self._async_with_connection(operation, deadline=deadline)
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
        deadline = asyncio.get_running_loop().time() + timeout
        self._cancel_idle_disconnect()
        await self._async_wait_for_idle_disconnect(deadline)
        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for purifier response"
            ) from err
        try:
            frames: list[bytes] = []
            loop = asyncio.get_running_loop()
            future: asyncio.Future[bytes] | None = None
            discard_connection = False

            def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
                nonlocal future
                if future is None or len(frames) >= len(requests):
                    return
                try:
                    frame = self._decode_application_frame(bytes(data))
                except ProtocolError as err:
                    if not future.done():
                        future.set_exception(err)
                    return
                matcher = requests[len(frames)][1]
                if not matcher(frame):
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
                nonlocal discard_connection, future
                primary_error: BaseException | None = None
                try:
                    await _async_wait_until(
                        client.start_notify(
                            self._profile.notify_char_uuid, notification_handler
                        ),
                        deadline,
                    )
                    for command, _matcher in requests:
                        future = loop.create_future()
                        await _async_wait_until(
                            client.write_gatt_char(
                                self._profile.write_char_uuid,
                                self._encode_application_frame(command),
                                response=False,
                            ),
                            deadline,
                        )
                        frames.append(await _async_wait_until(future, deadline))
                    return tuple(frames)
                except (TimeoutError, asyncio.TimeoutError) as err:
                    primary_error = GoveeBleClientError(
                        "Timed out waiting for purifier response"
                    )
                    raise primary_error from err
                except BaseException as err:
                    primary_error = err
                    raise
                finally:
                    if future is not None and not future.done():
                        future.cancel()
                    try:
                        await _async_wait_until(
                            client.stop_notify(self._profile.notify_char_uuid), deadline
                        )
                    except Exception:
                        discard_connection = True
                        _LOGGER.debug(
                            "Suppressing BLE notification cleanup failure%s",
                            " to preserve primary error" if primary_error else "",
                            exc_info=True,
                        )

            result = await self._async_with_connection(operation, deadline=deadline)
            if discard_connection:
                self._cancel_idle_disconnect()
                await self._async_drop_connection(deadline)
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
            deadline = asyncio.get_running_loop().time() + DEFAULT_TIMEOUT
        if self._closed:
            raise GoveeBleClientError("BLE client is closed")

        client = self._client
        if client is None or not client.is_connected:
            self._client = None
            self._session_key = None
            client = await transport.async_establish_connection(
                self._hass,
                self._address,
                self._handle_disconnect,
                deadline=deadline,
            )
            self._client = client
            if not client.is_connected:
                await self._async_drop_connection(deadline)
                raise GoveeBleClientError("Purifier disconnected while connecting")
            if self._profile.encryption is EncryptionMode.GOVEE_V1:
                try:
                    session_key = await self._async_negotiate_govee_v1_session(
                        client, deadline
                    )
                    if self._client is not client or not client.is_connected:
                        raise GoveeBleClientError(
                            "Purifier disconnected during encrypted-session setup"
                        )
                    self._session_key = session_key
                except (TimeoutError, asyncio.TimeoutError) as err:
                    await self._async_drop_connection(deadline)
                    raise GoveeBleClientError(
                        "Timed out establishing encrypted purifier session"
                    ) from err
                except GoveeBleClientError:
                    await self._async_drop_connection(deadline)
                    raise
                except Exception as err:
                    await self._async_drop_connection(deadline)
                    raise GoveeBleClientError(
                        "Failed to establish encrypted purifier session"
                    ) from err
                except BaseException:
                    await self._async_drop_connection(deadline)
                    raise

        try:
            result = await operation(client)
        except (TimeoutError, asyncio.TimeoutError) as err:
            await self._async_drop_connection(deadline)
            raise GoveeBleClientError(
                "Timed out waiting for purifier response"
            ) from err
        except BaseException:
            await self._async_drop_connection(deadline)
            raise

        self._schedule_idle_disconnect()
        return result

    def _handle_disconnect(self, client: Any) -> None:
        """Forget only the connection that actually disconnected."""

        if self._client is client:
            self._client = None
            self._session_key = None

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
        future: asyncio.Future[bytes] | None = None
        expected_command = 0
        primary_error: BaseException | None = None

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
            await _async_wait_until(
                client.start_notify(
                    self._profile.notify_char_uuid, notification_handler
                ),
                deadline,
            )

            expected_command = 0x01
            session_request = build_handshake_request(expected_command)
            future = loop.create_future()
            await _async_wait_until(
                client.write_gatt_char(
                    self._profile.write_char_uuid,
                    encrypt_frame(session_request, COMMUNICATION_KEY),
                    response=False,
                ),
                deadline,
            )
            session_response = await _async_wait_until(future, deadline)
            session_key = parse_session_key(session_response)

            expected_command = 0x02
            confirmation_request = build_handshake_request(expected_command)
            future = loop.create_future()
            await _async_wait_until(
                client.write_gatt_char(
                    self._profile.write_char_uuid,
                    encrypt_frame(confirmation_request, COMMUNICATION_KEY),
                    response=False,
                ),
                deadline,
            )
            confirmation_response = await _async_wait_until(future, deadline)
            validate_handshake_confirmation(
                confirmation_response, confirmation_request
            )
            return session_key
        except BaseException as err:
            primary_error = err
            raise
        finally:
            if future is not None and not future.done():
                future.cancel()
            try:
                await _async_wait_until(
                    client.stop_notify(self._profile.notify_char_uuid), deadline
                )
            except Exception:
                _LOGGER.debug(
                    "Suppressing encrypted-session notification cleanup failure%s",
                    " to preserve primary error" if primary_error else "",
                    exc_info=True,
                )
                if primary_error is None:
                    raise

    def _cancel_idle_disconnect(self) -> None:
        """Cancel an idle timer that has not started disconnecting."""

        if self._idle_disconnect_handle is not None:
            self._idle_disconnect_handle.cancel()
            self._idle_disconnect_handle = None

    async def _async_wait_for_idle_disconnect(self, deadline: float) -> None:
        """Wait for an idle disconnect that already won the scheduling race."""

        task = self._idle_disconnect_task
        if task is None or task is asyncio.current_task():
            return
        try:
            await _async_wait_until(asyncio.shield(task), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for purifier response"
            ) from err

    def _schedule_idle_disconnect(self) -> None:
        """Release a healthy connection after an idle period."""

        self._cancel_idle_disconnect()
        if self._closed:
            return
        loop = asyncio.get_running_loop()
        self._idle_disconnect_handle = loop.call_later(
            self._connection_idle_timeout, self._start_idle_disconnect
        )

    def _start_idle_disconnect(self) -> None:
        """Start serialized idle cleanup when its timer expires."""

        self._idle_disconnect_handle = None
        if self._closed or self._idle_disconnect_task is not None:
            return

        async def disconnect_idle_client() -> None:
            try:
                async with self._lock:
                    deadline = (
                        asyncio.get_running_loop().time() + DISCONNECT_TIMEOUT
                    )
                    await self._async_drop_connection(deadline)
            finally:
                if self._idle_disconnect_task is asyncio.current_task():
                    self._idle_disconnect_task = None

        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            task = self._hass.async_create_task(disconnect_idle_client())
        else:
            task = asyncio.create_task(disconnect_idle_client())
        self._idle_disconnect_task = task

    async def _async_drop_connection(self, deadline: float) -> None:
        """Forget and best-effort disconnect the cached connection."""

        client = self._client
        self._client = None
        self._session_key = None
        if client is not None:
            await transport.async_disconnect(client, deadline=deadline)

    async def async_close(self) -> None:
        """Cancel idle cleanup and close the cached connection."""

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
            _LOGGER.debug("Timed out waiting to close BLE client", exc_info=True)
            return
        try:
            await self._async_drop_connection(deadline)
        finally:
            self._lock.release()
