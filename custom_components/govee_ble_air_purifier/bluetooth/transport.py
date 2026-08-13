"""Home Assistant Bluetooth connection transport."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import hashlib
import logging
from typing import Any, TypeVar

from . import GoveeBleClientError

_LOGGER = logging.getLogger(__name__)
_T = TypeVar("_T")
_ACTIVE_SCAN_TASKS: set[asyncio.Task[None]] = set()
_ABANDONED_CONNECTION_ATTEMPTS: set[asyncio.Task[Any]] = set()
FRESH_ADVERTISEMENT_TIMEOUT = 10
FRESH_ADVERTISEMENT_POLL_INTERVAL = 0.1
# A distant purifier may need several connection intervals before its GATT
# session is established.  Keep this finite: callers use the deadline to let
# the shared arbiter move on if a device remains unavailable.
CONNECTION_TIMEOUT = 45.0
MAX_CONNECTION_ATTEMPTS = 3
RECOVERY_ACTIVE_SCAN_DURATION = FRESH_ADVERTISEMENT_TIMEOUT + CONNECTION_TIMEOUT


def device_log_id(address: str) -> str:
    """Return a stable short device identifier for integration-owned logs."""

    return hashlib.sha256(address.casefold().encode()).hexdigest()[:8]


def _client_log_suffix(client: Any) -> str:
    """Return a redacted log suffix when a transport exposes its address."""

    address = getattr(client, "address", None)
    if not isinstance(address, str):
        return ""
    return f" for device {device_log_id(address)}"


async def _async_wait_until(awaitable: Awaitable[_T], deadline: float) -> _T:
    """Await one stage without extending the transaction deadline."""

    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    return await asyncio.wait_for(awaitable, remaining)


def _observe_abandoned_connection_attempt(task: asyncio.Task[Any]) -> None:
    """Observe a late connection result without leaking a proxy slot."""

    _ABANDONED_CONNECTION_ATTEMPTS.discard(task)
    if task.cancelled():
        return
    try:
        client = task.result()
    except Exception:
        _LOGGER.debug("Late BLE connection attempt ended after cancellation", exc_info=True)
        return

    async def disconnect_late_client() -> None:
        await async_disconnect(
            client,
            deadline=asyncio.get_running_loop().time() + CONNECTION_TIMEOUT,
        )

    cleanup = asyncio.create_task(disconnect_late_client())
    _ACTIVE_SCAN_TASKS.add(cleanup)
    cleanup.add_done_callback(_ACTIVE_SCAN_TASKS.discard)


def _abandon_connection_attempt(task: asyncio.Task[Any]) -> None:
    """Cancel a non-cooperative connection attempt without awaiting it."""

    if task.done():
        return
    _ABANDONED_CONNECTION_ATTEMPTS.add(task)
    task.add_done_callback(_observe_abandoned_connection_attempt)
    task.cancel()


def clear_advertisement_history(hass: Any, address: str) -> None:
    """Allow the first unchanged advertisement after a GATT session to dispatch."""

    try:
        from homeassistant.components import bluetooth
    except ModuleNotFoundError:  # pragma: no cover - runtime dependency
        return

    clear_history = getattr(bluetooth, "async_clear_advertisement_history", None)
    if clear_history is None:
        return
    try:
        clear_history(hass, address)
    except Exception:
        _LOGGER.debug("Unable to clear Bluetooth advertisement history", exc_info=True)


async def _async_wait_for_fresh_legacy_path(
    bluetooth: Any, hass: Any, address: str, threshold: float
) -> None:
    """Wait for old Home Assistant history to observe a newer advertisement."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + FRESH_ADVERTISEMENT_TIMEOUT
    while True:
        latest = bluetooth.async_last_service_info(hass, address, connectable=True)
        paths = bluetooth.async_scanner_devices_by_address(
            hass, address, connectable=True
        )
        if paths and latest is not None and getattr(latest, "time", 0.0) > threshold:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(FRESH_ADVERTISEMENT_POLL_INTERVAL, remaining))


async def _async_wait_for_connectable_path(
    bluetooth: Any, hass: Any, address: str, deadline: float
) -> int:
    """Wait for scanner inventory to catch up with an advertisement callback."""

    loop = asyncio.get_running_loop()
    while True:
        paths = bluetooth.async_scanner_devices_by_address(
            hass, address, connectable=True
        )
        if paths:
            return len(paths)
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError
        await asyncio.sleep(min(FRESH_ADVERTISEMENT_POLL_INTERVAL, remaining))


def _start_active_scan(bluetooth: Any, hass: Any) -> None:
    """Start a bounded Active window that outlives advertisement discovery."""

    request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
    if request_active_scan is None:
        return

    async def request_scan() -> None:
        try:
            await request_active_scan(hass, RECOVERY_ACTIVE_SCAN_DURATION)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.debug("Unable to extend active Bluetooth scan", exc_info=True)

    if create_background_task := getattr(hass, "async_create_background_task", None):
        task = create_background_task(request_scan(), "Govee BLE recovery active scan")
    else:
        create_task = getattr(hass, "async_create_task", asyncio.create_task)
        task = create_task(request_scan())
    _ACTIVE_SCAN_TASKS.add(task)
    task.add_done_callback(_ACTIVE_SCAN_TASKS.discard)


async def async_prepare_connection_path(
    hass: Any,
    address: str,
    *,
    after: float | None,
    wait_for_advertisement: bool = True,
) -> None:
    """Ensure a fresh connectable path exists before a transaction starts."""

    log_id = device_log_id(address)

    try:
        from homeassistant.components import bluetooth
    except ModuleNotFoundError as err:  # pragma: no cover - runtime dependency
        raise GoveeBleClientError(
            "Home Assistant BLE dependencies are unavailable"
        ) from err

    paths = bluetooth.async_scanner_devices_by_address(hass, address, connectable=True)
    can_clear_history = hasattr(bluetooth, "async_clear_advertisement_history")
    if paths:
        if after is None:
            return
        latest = bluetooth.async_last_service_info(hass, address, connectable=True)
        if latest is not None and getattr(latest, "time", 0.0) > after:
            return

    if not wait_for_advertisement:
        raise GoveeBleClientError(
            f"No fresh connectable Bluetooth path is available for device {log_id}"
        )

    _start_active_scan(bluetooth, hass)
    threshold = after if after is not None else asyncio.get_running_loop().time()
    _LOGGER.debug(
        "No fresh connectable path for device %s; waiting for a new advertisement",
        log_id,
    )
    if not can_clear_history:
        # Home Assistant 2024.8 updates history timestamps before suppressing
        # unchanged callback payloads, so poll that public state for freshness.
        try:
            await _async_wait_for_fresh_legacy_path(bluetooth, hass, address, threshold)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for a fresh purifier advertisement"
            ) from err
        return

    clear_advertisement_history(hass, address)
    path_deadline = asyncio.get_running_loop().time() + FRESH_ADVERTISEMENT_TIMEOUT
    try:
        await bluetooth.async_process_advertisements(
            hass,
            lambda service_info: getattr(service_info, "time", 0.0) > threshold,
            {"address": address, "connectable": True},
            bluetooth.BluetoothScanningMode.ACTIVE,
            FRESH_ADVERTISEMENT_TIMEOUT,
        )
    except (TimeoutError, asyncio.TimeoutError) as err:
        raise GoveeBleClientError(
            "Timed out waiting for a fresh purifier advertisement"
        ) from err

    try:
        path_count = await _async_wait_for_connectable_path(
            bluetooth, hass, address, path_deadline
        )
    except (TimeoutError, asyncio.TimeoutError) as err:
        raise GoveeBleClientError(
            f"No connectable Bluetooth path was found for device {log_id} "
            "after a fresh advertisement"
        ) from err
    _LOGGER.debug(
        "Fresh purifier advertisement produced %d connectable path(s)", path_count
    )


async def async_establish_connection(
    hass: Any,
    address: str,
    disconnected_callback: Callable[[Any], None],
    *,
    deadline: float,
) -> Any:
    """Establish a connection through Home Assistant Bluetooth helpers."""

    started = asyncio.get_running_loop().time()
    log_id = device_log_id(address)
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
        _LOGGER.debug("BLE connection stage: %s (device %s)", stage, log_id)
        ble_device = bluetooth.async_ble_device_from_address(
            hass, address, connectable=True
        )
        if ble_device is None:
            raise GoveeBleClientError(f"BLE device {log_id} is not available")

        stage = "closing stale connections"
        _LOGGER.debug("BLE connection stage: %s (device %s)", stage, log_id)
        await _async_wait_until(close_stale_connections(ble_device), deadline)
        stage = "establishing a new connection"
        _LOGGER.debug("BLE connection stage: %s (device %s)", stage, log_id)
        attempt = asyncio.create_task(
            establish_connection(
                client_class=BleakClientWithServiceCache,
                device=ble_device,
                name=ble_device.name or address,
                disconnected_callback=disconnected_callback,
                max_attempts=MAX_CONNECTION_ATTEMPTS,
            )
        )
        remaining = max(0.0, deadline - asyncio.get_running_loop().time())
        done, _pending = await asyncio.wait((attempt,), timeout=remaining)
        if attempt not in done:
            _abandon_connection_attempt(attempt)
            raise TimeoutError
        client = await attempt
        _LOGGER.debug(
            "BLE connection established in %.2f seconds (device %s)",
            asyncio.get_running_loop().time() - started,
            log_id,
        )
        return client
    except (TimeoutError, asyncio.TimeoutError) as err:
        _LOGGER.debug(
            "BLE connection timed out while %s after %.2f seconds (device %s)",
            stage,
            asyncio.get_running_loop().time() - started,
            log_id,
            exc_info=True,
        )
        raise GoveeBleClientError(
            "Timed out establishing Bluetooth connection"
        ) from err
    except asyncio.CancelledError:
        if "attempt" in locals():
            _abandon_connection_attempt(attempt)
        raise


async def async_disconnect(client: Any, *, deadline: float) -> None:
    """Disconnect without allowing cleanup failure to escape."""

    log_suffix = _client_log_suffix(client)
    try:
        _LOGGER.debug("Disconnecting BLE client%s", log_suffix)
        await _async_wait_until(client.disconnect(), deadline)
        _LOGGER.debug("BLE client disconnected%s", log_suffix)
    except Exception:
        _LOGGER.debug("Suppressing BLE disconnect failure%s", log_suffix, exc_info=True)
