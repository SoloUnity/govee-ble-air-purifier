"""BLE client for Govee H7124-style air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
import logging
from typing import Any

from .coordinator import GoveeData
from .profiles import H7124_PROFILE, ModelProfile
from .protocol import (
    ProtocolError,
    is_fan_mode_confirmation,
    is_power_confirmation,
    validate_frame,
)

DEFAULT_TIMEOUT = 10.0
COMMAND_CONFIRMATION_TIMEOUT = 2.0
_LOGGER = logging.getLogger(__name__)


class GoveeBleClientError(Exception):
    """Raised when BLE communication fails."""


class GoveeBleClient:
    """Small serialized request/response BLE client."""

    def __init__(
        self, hass: Any, address: str, *, profile: ModelProfile = H7124_PROFILE
    ) -> None:
        self._hass = hass
        self._address = address
        self._profile = profile
        self._lock = asyncio.Lock()

    async def async_get_state(self) -> GoveeData:
        """Poll power, PM2.5, and filter-life state."""

        power_frame, status_frame = await self._async_write_and_wait_many(
            (
                (self._profile.state_query_command, self._profile.is_power_state_response),
                (self._profile.status_query_command, self._profile.is_status_response),
            )
        )
        status = self._profile.decode_status(status_frame)
        return GoveeData(
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

    async def async_set_power_and_fan_mode(self, mode: str) -> GoveeData:
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
        return GoveeData(
            is_on=self._profile.decode_power_state(power_frame),
            fan_mode=mode,
        )

    async def _async_write_without_response(self, command: bytes) -> None:
        async with self._lock:
            await self._async_write_commands_without_response((command,))

    async def _async_write_commands_without_response(
        self, commands: tuple[bytes, ...]
    ) -> None:
        async def operation(client: Any) -> None:
            for command in commands:
                await client.write_gatt_char(
                    self._profile.write_char_uuid, command, response=False
                )

        await self._async_with_connection(operation)

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
        async with self._lock:
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
                await client.start_notify(
                    self._profile.notify_char_uuid, notification_handler
                )
                primary_error: BaseException | None = None
                try:
                    for command, _matcher in requests:
                        future = loop.create_future()
                        await client.write_gatt_char(
                            self._profile.write_char_uuid, command, response=False
                        )
                        frames.append(await asyncio.wait_for(future, timeout))
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
                    cleanup_context = (
                        contextlib.suppress(Exception)
                        if primary_error
                        else contextlib.nullcontext()
                    )
                    with cleanup_context:
                        await client.stop_notify(self._profile.notify_char_uuid)

            return await self._async_with_connection(operation)

    async def _async_with_connection(self, operation: Callable[[Any], Any]) -> Any:
        """Connect with HA Bluetooth helpers and run a BLE operation."""

        try:
            from bleak_retry_connector import (
                BleakClientWithServiceCache,
                close_stale_connections,
                establish_connection,
            )
            from homeassistant.components import bluetooth
        except ModuleNotFoundError as err:  # pragma: no cover - runtime dependency
            raise GoveeBleClientError("Home Assistant BLE dependencies are unavailable") from err

        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise GoveeBleClientError(f"BLE device {self._address} is not available")

        await close_stale_connections(ble_device)
        client = await establish_connection(
            client_class=BleakClientWithServiceCache,
            device=ble_device,
            name=ble_device.name or self._address,
        )
        primary_error: BaseException | None = None
        try:
            return await operation(client)
        except TimeoutError as err:
            primary_error = GoveeBleClientError(
                "Timed out waiting for purifier response"
            )
            raise primary_error from err
        except asyncio.TimeoutError as err:
            primary_error = GoveeBleClientError(
                "Timed out waiting for purifier response"
            )
            raise primary_error from err
        except BaseException as err:
            primary_error = err
            raise
        finally:
            try:
                await client.disconnect()
            except Exception:
                if primary_error is None:
                    _LOGGER.debug(
                        "Suppressing BLE disconnect failure after successful operation",
                        exc_info=True,
                    )
                else:
                    _LOGGER.debug(
                        "Suppressing BLE disconnect failure to preserve primary error",
                        exc_info=True,
                    )
