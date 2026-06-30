import importlib
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.coordinator import GoveeData


@pytest.mark.asyncio
async def test_diagnostics_reads_runtime_data_before_legacy_hass_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_entries_module = ModuleType("homeassistant.config_entries")
    config_entries_module.ConfigEntry = object
    core_module = ModuleType("homeassistant.core")
    core_module.HomeAssistant = object
    homeassistant_module = ModuleType("homeassistant")
    homeassistant_module.config_entries = config_entries_module
    homeassistant_module.core = core_module
    monkeypatch.setitem(__import__("sys").modules, "homeassistant", homeassistant_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "homeassistant.config_entries",
        config_entries_module,
    )
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.core", core_module)

    diagnostics = importlib.reload(
        importlib.import_module("custom_components.govee_ble_air_purifier.diagnostics")
    )
    runtime_coordinator = SimpleNamespace(
        data=GoveeData(is_on=True, fan_mode="Auto", pm25=9, filter_life=91)
    )
    legacy_coordinator = SimpleNamespace(
        data=GoveeData(is_on=False, fan_mode="Sleep", pm25=99, filter_life=1)
    )
    entry = SimpleNamespace(
        data={"address": "aa:bb:cc:dd:ee:ff"},
        entry_id="entry-1",
        runtime_data=SimpleNamespace(coordinator=runtime_coordinator),
    )
    hass = SimpleNamespace(
        data={diagnostics.DOMAIN: {entry.entry_id: legacy_coordinator}}
    )

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result == {
        "entry": {"address": "XX:XX:XX:DD:EE:FF"},
        "state": {
            "is_on": True,
            "fan_mode": "Auto",
            "pm25": 9,
            "filter_life": 91,
        },
    }
