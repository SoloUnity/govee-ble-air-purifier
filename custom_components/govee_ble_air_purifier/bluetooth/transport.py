"""Home Assistant Bluetooth connection transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
from typing import Any, TypeVar

from . import GoveeBleClientError

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")


async def _async_wait_until(awaitable: Awaitable[_T], deadline: float) -> _T:
    """Await one stage without extending the transaction deadline."""

    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    return await asyncio.wait_for(awaitable, remaining)


async def async_establish_connection(
    hass: Any,
    address: str,
    disconnected_callback: Callable[[Any], None],
    *,
    deadline: float,
) -> Any:
    """Establish a connection through Home Assistant Bluetooth helpers."""

    started = asyncio.get_running_loop().time()
    stage = "loading Home Assistant Bluetooth helpers"

    try:
        from bleak_retry_connector import (
            BleakClientWithServiceCache,
            close_stale_connections,
            establish_connection,
        )
        from homeassistant.components import bluetooth
    except ModuleNotFoundError as err:  # pragma: no cover - runtime dependency
        raise GoveeBleClientError(
            "Home Assistant BLE dependencies are unavailable"
        ) from err

    try:
        stage = "looking up a connectable BLE device"
        _LOGGER.debug("BLE connection stage: %s", stage)
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            raise GoveeBleClientError(f"BLE device {address} is not available")

        stage = "closing stale connections"
        _LOGGER.debug("BLE connection stage: %s", stage)
        await _async_wait_until(close_stale_connections(ble_device), deadline)
        stage = "establishing a new connection"
        _LOGGER.debug("BLE connection stage: %s", stage)
        client = await _async_wait_until(
            establish_connection(
                client_class=BleakClientWithServiceCache,
                device=ble_device,
                name=ble_device.name or address,
                disconnected_callback=disconnected_callback,
            ),
            deadline,
        )
        _LOGGER.debug(
            "BLE connection established in %.2f seconds",
            asyncio.get_running_loop().time() - started,
        )
        return client
    except (TimeoutError, asyncio.TimeoutError) as err:
        _LOGGER.debug(
            "BLE connection timed out while %s after %.2f seconds",
            stage,
            asyncio.get_running_loop().time() - started,
            exc_info=True,
        )
        raise GoveeBleClientError("Timed out waiting for purifier response") from err


async def async_disconnect(client: Any, *, deadline: float) -> None:
    """Disconnect without allowing cleanup failure to escape."""

    try:
        _LOGGER.debug("Disconnecting BLE client")
        await _async_wait_until(client.disconnect(), deadline)
        _LOGGER.debug("BLE client disconnected")
    except Exception:
        _LOGGER.debug("Suppressing BLE disconnect failure", exc_info=True)
