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
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            raise GoveeBleClientError(f"BLE device {address} is not available")

        await _async_wait_until(close_stale_connections(ble_device), deadline)
        return await _async_wait_until(
            establish_connection(
                client_class=BleakClientWithServiceCache,
                device=ble_device,
                name=ble_device.name or address,
                disconnected_callback=disconnected_callback,
            ),
            deadline,
        )
    except (TimeoutError, asyncio.TimeoutError) as err:
        raise GoveeBleClientError("Timed out waiting for purifier response") from err


async def async_disconnect(client: Any, *, deadline: float) -> None:
    """Disconnect without allowing cleanup failure to escape."""

    try:
        await _async_wait_until(client.disconnect(), deadline)
    except Exception:
        _LOGGER.debug("Suppressing BLE disconnect failure", exc_info=True)
