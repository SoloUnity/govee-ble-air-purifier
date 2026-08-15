import importlib
import sys
from dataclasses import replace
from enum import StrEnum
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.models import (
    NightLightState,
    PurifierState,
)
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE
from tests.helpers.ha_stubs import (
    CoordinatorEntity as _CoordinatorEntity,
    DeviceInfo as _DeviceInfo,
    HomeAssistantError as _HomeAssistantError,
    install_modules,
)

MODULE_NAME = "custom_components.govee_ble_air_purifier.light"


class _ColorMode(StrEnum):
    RGB = "rgb"


class _LightEntity:
    pass


class _FakeCoordinator:
    profile = H7124_PROFILE

    def __init__(self) -> None:
        self.data = PurifierState(
            night_light=NightLightState(
                is_on=True,
                brightness_percent=50,
                rgb_color=(255, 0, 0),
            )
        )
        self.last_update_success = True
        self.commands: list[dict] = []
        self.fail = False

    async def async_set_night_light(self, **kwargs) -> None:
        self.commands.append(kwargs)
        if self.fail:
            raise RuntimeError("BLE write failed")


def _import_light(monkeypatch: pytest.MonkeyPatch):
    install_modules(
        monkeypatch,
        {
            "homeassistant.components.light": {
                "ATTR_BRIGHTNESS": "brightness",
                "ATTR_RGB_COLOR": "rgb_color",
                "ColorMode": _ColorMode,
                "LightEntity": _LightEntity,
            },
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.core": {"HomeAssistant": object},
            "homeassistant.exceptions": {"HomeAssistantError": _HomeAssistantError},
            "homeassistant.helpers.device_registry": {
                "CONNECTION_BLUETOOTH": "bluetooth",
                "DeviceInfo": _DeviceInfo,
            },
            "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
            "homeassistant.helpers.update_coordinator": {
                "CoordinatorEntity": _CoordinatorEntity
            },
        },
    )

    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


def _entry(coordinator: _FakeCoordinator) -> SimpleNamespace:
    return SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            profile=coordinator.profile,
        ),
    )


@pytest.mark.asyncio
async def test_light_setup_is_gated_by_profile_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = _import_light(monkeypatch)
    coordinator = _FakeCoordinator()
    added_entities = []

    await light.async_setup_entry(object(), _entry(coordinator), added_entities.extend)

    assert len(added_entities) == 1
    entity = added_entities[0]
    assert isinstance(entity, light.GoveeNightLight)
    assert entity._attr_unique_id == "aabbccddeeff_night_light"
    assert entity._attr_translation_key == "night_light"
    assert entity._attr_supported_color_modes == {_ColorMode.RGB}

    coordinator.profile = replace(H7124_PROFILE, night_light=None)
    added_entities.clear()

    await light.async_setup_entry(object(), _entry(coordinator), added_entities.extend)

    assert added_entities == []


def test_light_reports_cached_state_and_availability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = _import_light(monkeypatch)
    coordinator = _FakeCoordinator()
    entity = light.GoveeNightLight(coordinator, _entry(coordinator))

    assert entity.is_on is True
    assert entity.color_mode is _ColorMode.RGB
    assert entity.brightness == 128
    assert entity.rgb_color == (255, 0, 0)
    assert entity.available is True

    coordinator.data = PurifierState(
        night_light=NightLightState(
            is_on=False,
            brightness_percent=1,
            rgb_color=(0, 0, 255),
        )
    )
    coordinator.last_update_success = False

    assert entity.is_on is False
    assert entity.color_mode is None
    assert entity.brightness == 3
    assert entity.rgb_color == (0, 0, 255)
    assert entity.available is False


@pytest.mark.parametrize(
    ("brightness", "percent", "reported_brightness"),
    [(1, 1, 3), (128, 50, 128), (255, 100, 255)],
)
def test_brightness_conversion_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    brightness: int,
    percent: int,
    reported_brightness: int,
) -> None:
    light = _import_light(monkeypatch)

    assert light._brightness_to_percent(brightness) == percent
    assert light._percent_to_brightness(percent) == reported_brightness


@pytest.mark.asyncio
async def test_turn_on_processes_brightness_and_rgb_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = _import_light(monkeypatch)
    coordinator = _FakeCoordinator()
    entity = light.GoveeNightLight(coordinator, _entry(coordinator))

    await entity.async_turn_on(brightness=128, rgb_color=(255, 255, 0))

    assert coordinator.commands == [
        {
            "is_on": True,
            "brightness_percent": 50,
            "rgb_color": (255, 255, 0),
        }
    ]


@pytest.mark.asyncio
async def test_turn_on_without_settings_and_brightness_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = _import_light(monkeypatch)
    coordinator = _FakeCoordinator()
    entity = light.GoveeNightLight(coordinator, _entry(coordinator))

    await entity.async_turn_on()
    await entity.async_turn_on(brightness=0, rgb_color=(0, 255, 0))
    await entity.async_turn_off()

    assert coordinator.commands == [
        {"is_on": True, "brightness_percent": None, "rgb_color": None},
        {"is_on": False},
        {"is_on": False},
    ]


@pytest.mark.asyncio
async def test_light_commands_raise_home_assistant_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    light = _import_light(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.fail = True
    entity = light.GoveeNightLight(coordinator, _entry(coordinator))

    with pytest.raises(_HomeAssistantError, match="Failed to turn night light on"):
        await entity.async_turn_on(rgb_color=(0, 255, 0))
    with pytest.raises(_HomeAssistantError, match="Failed to turn night light off"):
        await entity.async_turn_off()
