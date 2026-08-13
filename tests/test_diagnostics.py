import importlib
import sys
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.models import (
    NightLightState,
    PurifierState,
)
from tests.helpers.ha_stubs import install_modules


def _import_diagnostics(monkeypatch: pytest.MonkeyPatch):
    install_modules(
        monkeypatch,
        {
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.core": {"HomeAssistant": object},
        },
    )
    sys.modules.pop("custom_components.govee_ble_air_purifier.diagnostics", None)
    return importlib.import_module(
        "custom_components.govee_ble_air_purifier.diagnostics"
    )


@pytest.mark.asyncio
async def test_diagnostics_reads_runtime_data_before_legacy_hass_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _import_diagnostics(monkeypatch)
    runtime_coordinator = SimpleNamespace(
        data=PurifierState(
            is_on=True,
            fan_mode="Auto",
            pm25=9,
            filter_life=91,
            night_light=NightLightState(
                is_on=True,
                brightness_percent=50,
                rgb_color=(255, 255, 0),
            ),
        )
    )
    legacy_coordinator = SimpleNamespace(
        data=PurifierState(is_on=False, fan_mode="Sleep", pm25=99, filter_life=1)
    )
    entry = SimpleNamespace(
        data={"address": "aa:bb:cc:dd:ee:ff", "name": "GVH7124ABCD"},
        options={
            "use_custom_auto": True,
            "custom_auto_threshold_40": 3,
            "share_bluetooth_connection": True,
        },
        entry_id="entry-1",
        runtime_data=SimpleNamespace(
            coordinator=runtime_coordinator,
            controller=SimpleNamespace(
                diagnostics=lambda: {
                    "active": True,
                    "current_speed": 80,
                }
            ),
            auto_resume=SimpleNamespace(
                diagnostics=lambda: {
                    "mode": "custom_auto",
                    "suspended": False,
                    "custom_speed": 80,
                    "reconcile_pending": False,
                }
            ),
        ),
    )
    hass = SimpleNamespace(
        data={diagnostics.DOMAIN: {entry.entry_id: legacy_coordinator}}
    )

    result = await diagnostics.async_get_config_entry_diagnostics(hass, entry)

    assert result == {
        "entry": {"address": "XX:XX:XX:XX:XX:XX", "name": "REDACTED"},
        "options": {
            "use_custom_auto": True,
            "custom_auto_threshold_40": 3,
            "share_bluetooth_connection": True,
        },
        "connection_mode": "shared",
        "state": {
            "is_on": True,
            "fan_mode": "Auto",
            "pm25": 9,
            "filter_life": 91,
            "night_light": {
                "is_on": True,
                "brightness_percent": 50,
                "rgb_color": [255, 255, 0],
            },
        },
        "custom_auto": {
            "active": True,
            "current_speed": 80,
        },
        "auto_resume": {
            "mode": "custom_auto",
            "suspended": False,
            "custom_speed": 80,
            "reconcile_pending": False,
        },
    }


def test_diagnostics_redacts_all_uuid_address_components(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _import_diagnostics(monkeypatch)

    assert diagnostics._redact_address("a1b2c3d4-e5f6-47a8-9012-123456789abc") == (
        "XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX"
    )


@pytest.mark.asyncio
async def test_diagnostics_redacts_arbitrary_user_provided_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    diagnostics = _import_diagnostics(monkeypatch)
    entry = SimpleNamespace(
        data={"address": "AA:BB:CC:DD:EE:FF", "name": "Gordon's Bedroom"},
        options={},
        entry_id="entry-1",
        runtime_data=None,
    )

    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={}), entry
    )

    assert result == {
        "entry": {"address": "XX:XX:XX:XX:XX:XX", "name": "REDACTED"},
        "options": {},
        "connection_mode": "dedicated",
        "state": None,
        "custom_auto": None,
        "auto_resume": None,
    }
