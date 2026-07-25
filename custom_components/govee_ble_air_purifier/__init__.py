"""Govee BLE Air Purifier integration."""

from __future__ import annotations

from contextlib import suppress
from datetime import timedelta
from typing import Any

from .bluetooth.client import GoveeBleClient
from .const import CONF_ADDRESS, CONF_PROFILE, PLATFORMS
from .coordinator import GoveeCoordinator, GoveeRuntimeData
from .custom_auto.config import CustomAutoConfig
from .custom_auto.controller import CustomAutoController
from .profiles import get_profile
from .setup_helpers import polling_interval_from_options


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Govee BLE Air Purifier from a config entry."""

    address = entry.data[CONF_ADDRESS]
    profile = get_profile(entry.data.get(CONF_PROFILE))
    client = GoveeBleClient(hass, address, profile=profile)
    coordinator = GoveeCoordinator(
        hass,
        client,
        profile=profile,
        polling_interval=timedelta(
            seconds=polling_interval_from_options(entry.options)
        ),
    )
    controller: CustomAutoController | None = None
    runtime_data: GoveeRuntimeData | None = None
    try:
        await coordinator.async_config_entry_first_refresh()
        controller = CustomAutoController(
            hass,
            coordinator,
            CustomAutoConfig.from_options(entry.options),
            config_entry=entry,
        )
        runtime_data = GoveeRuntimeData(
            coordinator=coordinator, profile=profile, controller=controller
        )
        entry.runtime_data = runtime_data
        entry.async_on_unload(entry.add_update_listener(_async_update_listener))
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        if controller is not None:
            with suppress(Exception):
                await controller.async_stop()
        with suppress(Exception):
            await coordinator.async_shutdown()
        if getattr(entry, "runtime_data", None) is runtime_data:
            entry.runtime_data = None
        raise
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.controller.async_stop()
        await entry.runtime_data.coordinator.async_shutdown()
    return unload_ok


async def _async_update_listener(hass: Any, entry: Any) -> None:
    """Reload the config entry when options change."""

    await hass.config_entries.async_reload(entry.entry_id)
