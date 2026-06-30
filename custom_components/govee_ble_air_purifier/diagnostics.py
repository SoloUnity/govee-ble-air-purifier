"""Diagnostics support for Govee BLE Air Purifier."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_ADDRESS, DOMAIN


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    data = dict(entry.data)
    if CONF_ADDRESS in data:
        data[CONF_ADDRESS] = _redact_address(data[CONF_ADDRESS])

    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None)
    if coordinator is None:
        coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    state = None
    if coordinator is not None and coordinator.data is not None:
        state = {
            "is_on": coordinator.data.is_on,
            "fan_mode": coordinator.data.fan_mode,
            "pm25": coordinator.data.pm25,
            "filter_life": coordinator.data.filter_life,
        }
    return {"entry": data, "state": state}


def _redact_address(address: str) -> str:
    """Redact a BLE/MAC address while preserving troubleshooting shape."""

    normalized = address.upper().replace("-", ":")
    parts = normalized.split(":")
    if len(parts) == 6:
        return "XX:XX:XX:" + ":".join(parts[3:])
    return "REDACTED"
