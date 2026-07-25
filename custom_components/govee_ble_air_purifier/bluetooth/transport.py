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


async def async_with_connection(
    hass: Any,
    address: str,
    operation: Callable[[Any], Awaitable[_T]],
    *,
    deadline: float,
) -> _T:
    """Connect with Home Assistant Bluetooth helpers and run an operation."""

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

    client: Any = None
    primary_error: BaseException | None = None
    try:
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            raise GoveeBleClientError(f"BLE device {address} is not available")

        await _async_wait_until(close_stale_connections(ble_device), deadline)
        client = await _async_wait_until(
            establish_connection(
                client_class=BleakClientWithServiceCache,
                device=ble_device,
                name=ble_device.name or address,
            ),
            deadline,
        )
        return await operation(client)
    except (TimeoutError, asyncio.TimeoutError) as err:
        primary_error = GoveeBleClientError(
            "Timed out waiting for purifier response"
        )
        raise primary_error from err
    except BaseException as err:
        primary_error = err
        raise
    finally:
        try:
            if client is not None:
                await _async_wait_until(client.disconnect(), deadline)
        except Exception:
            _LOGGER.debug(
                "Suppressing BLE disconnect failure%s",
                " to preserve primary error"
                if primary_error
                else " after successful operation",
                exc_info=True,
            )
