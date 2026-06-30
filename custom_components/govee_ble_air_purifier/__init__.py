"""Govee BLE Air Purifier integration."""

from __future__ import annotations

from typing import Any

from .client import GoveeBleClient
from .const import CONF_ADDRESS, CONF_PROFILE, PLATFORMS
from .coordinator import GoveeCoordinator, GoveeRuntimeData
from .profiles import get_profile


async def async_setup_entry(hass: Any, entry: Any) -> bool:
    """Set up Govee BLE Air Purifier from a config entry."""

    address = entry.data[CONF_ADDRESS]
    profile = get_profile(entry.data.get(CONF_PROFILE))
    client = GoveeBleClient(hass, address, profile=profile)
    coordinator = GoveeCoordinator(hass, client, profile=profile)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoveeRuntimeData(coordinator=coordinator, profile=profile)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: Any, entry: Any) -> bool:
    """Unload a config entry."""

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok
