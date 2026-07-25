"""BLE client for Govee H7124-style air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import logging
from typing import Any

from ..models import PurifierState
from ..profiles import H7124_PROFILE, ModelProfile
from ..protocol import (
    is_fan_mode_confirmation,
    is_power_confirmation,
)
from . import GoveeBleClientError, transport
from .framing import ProtocolError, validate_frame
from .transport import _async_wait_until

DEFAULT_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0
COMMAND_CONFIRMATION_TIMEOUT = 2.0
_LOGGER = logging.getLogger(__name__)


class GoveeBleClient:
    """Small serialized request/response BLE client."""

    def __init__(
        self, hass: Any, address: str, *, profile: ModelProfile = H7124_PROFILE
    ) -> None:
        self._hass = hass
        self._address = address
        self._profile = profile
        self._lock = asyncio.Lock()

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
        """Set purifier fan mode using canonical 3a05 commands."""

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
                        self._profile.write_char_uuid, command, response=False
                    ),
                    deadline,
                )

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

            def notification_handler(_sender: Any, data: bytearray | bytes) -> None:
                nonlocal future
                frame = bytes(data)
                if future is None or len(frames) >= len(requests):
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
                nonlocal future
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
                                self._profile.write_char_uuid, command, response=False
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
                        _LOGGER.debug(
                            "Suppressing BLE notification cleanup failure%s",
                            " to preserve primary error" if primary_error else "",
                            exc_info=True,
                        )

            return await self._async_with_connection(operation, deadline=deadline)
        finally:
            self._lock.release()

    async def _async_with_connection(
        self,
        operation: Callable[[Any], Any],
        *,
        deadline: float | None = None,
    ) -> Any:
        """Run an operation through the shared Bluetooth transport."""

        if deadline is None:
            deadline = asyncio.get_running_loop().time() + DEFAULT_TIMEOUT
        return await transport.async_with_connection(
            self._hass,
            self._address,
            operation,
            deadline=deadline,
        )
