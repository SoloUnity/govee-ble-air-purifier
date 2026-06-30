import importlib
import math
import sys
from enum import IntFlag
from types import ModuleType, SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.coordinator import GoveeData
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE


MODULE_NAME = "custom_components.govee_ble_air_purifier.fan"


class _FanEntityFeature(IntFlag):
    TURN_ON = 1
    TURN_OFF = 2
    SET_SPEED = 4
    PRESET_MODE = 8


class _CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator


class _HomeAssistantError(Exception):
    pass


class _DeviceInfo(dict):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)


class _FakeCoordinator:
    profile = H7124_PROFILE

    def __init__(self) -> None:
        self.data = GoveeData(is_on=True, pm25=7, filter_life=93, fan_mode="Low")
        self.power_commands: list[bool] = []
        self.fan_mode_commands: list[str] = []

    async def async_set_power(self, is_on: bool) -> None:
        self.power_commands.append(is_on)
        self.data = GoveeData(
            is_on=is_on,
            pm25=self.data.pm25 if self.data else None,
            filter_life=self.data.filter_life if self.data else None,
            fan_mode=self.data.fan_mode if is_on and self.data else None,
        )

    async def async_set_fan_mode(self, mode: str) -> None:
        self.fan_mode_commands.append(mode)
        self.data = GoveeData(is_on=True, pm25=7, filter_life=93, fan_mode=mode)


def _install_homeassistant_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    homeassistant_module = ModuleType("homeassistant")
    components_module = ModuleType("homeassistant.components")
    fan_module = ModuleType("homeassistant.components.fan")
    config_entries_module = ModuleType("homeassistant.config_entries")
    core_module = ModuleType("homeassistant.core")
    exceptions_module = ModuleType("homeassistant.exceptions")
    helpers_module = ModuleType("homeassistant.helpers")
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")
    update_coordinator_module = ModuleType("homeassistant.helpers.update_coordinator")
    util_module = ModuleType("homeassistant.util")
    percentage_module = ModuleType("homeassistant.util.percentage")

    def ordered_list_item_to_percentage(options: list[str], item: str) -> int:
        return round(((options.index(item) + 1) * 100) / len(options))

    def percentage_to_ordered_list_item(options: list[str], percentage: int) -> str:
        index = min(
            len(options) - 1,
            max(0, math.ceil((percentage * len(options)) / 100) - 1),
        )
        return options[index]

    fan_module.FanEntity = object
    fan_module.FanEntityFeature = _FanEntityFeature
    config_entries_module.ConfigEntry = object
    core_module.HomeAssistant = object
    exceptions_module.HomeAssistantError = _HomeAssistantError
    device_registry_module.DeviceInfo = _DeviceInfo
    entity_platform_module.AddEntitiesCallback = object
    update_coordinator_module.CoordinatorEntity = _CoordinatorEntity
    percentage_module.ordered_list_item_to_percentage = ordered_list_item_to_percentage
    percentage_module.percentage_to_ordered_list_item = percentage_to_ordered_list_item

    homeassistant_module.components = components_module
    homeassistant_module.config_entries = config_entries_module
    homeassistant_module.core = core_module
    homeassistant_module.exceptions = exceptions_module
    homeassistant_module.helpers = helpers_module
    homeassistant_module.util = util_module
    components_module.fan = fan_module
    helpers_module.device_registry = device_registry_module
    helpers_module.entity_platform = entity_platform_module
    helpers_module.update_coordinator = update_coordinator_module
    util_module.percentage = percentage_module

    modules = {
        "homeassistant": homeassistant_module,
        "homeassistant.components": components_module,
        "homeassistant.components.fan": fan_module,
        "homeassistant.config_entries": config_entries_module,
        "homeassistant.core": core_module,
        "homeassistant.exceptions": exceptions_module,
        "homeassistant.helpers": helpers_module,
        "homeassistant.helpers.device_registry": device_registry_module,
        "homeassistant.helpers.entity_platform": entity_platform_module,
        "homeassistant.helpers.update_coordinator": update_coordinator_module,
        "homeassistant.util": util_module,
        "homeassistant.util.percentage": percentage_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _import_fan(monkeypatch: pytest.MonkeyPatch):
    _install_homeassistant_modules(monkeypatch)
    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


def test_loaded_platforms_match_cloud_style_control_layout() -> None:
    from custom_components.govee_ble_air_purifier.const import PLATFORMS

    assert PLATFORMS == ["fan", "sensor"]


@pytest.mark.asyncio
async def test_fan_setup_creates_one_air_purifier_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    added_entities = []

    await fan.async_setup_entry(object(), entry, added_entities.extend)

    assert len(added_entities) == 1
    assert isinstance(added_entities[0], fan.GoveeAirPurifierFan)
    assert added_entities[0]._attr_name is None


@pytest.mark.asyncio
async def test_fan_entity_maps_power_speed_and_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry)

    assert entity.is_on is True
    # Low is the first of three gear speeds [Low, Medium, High].
    assert entity.percentage == 33
    assert entity.preset_mode == "Manual"
    assert entity._attr_preset_modes == ["Manual", "Auto", "Sleep", "Turbo"]
    assert entity._attr_speed_count == 3
    assert entity._manual_speeds == ["Low", "Medium", "High"]
    assert entity._mode_presets == ["Auto", "Sleep", "Turbo"]
    assert entity._attr_supported_features == (
        _FanEntityFeature.TURN_ON
        | _FanEntityFeature.TURN_OFF
        | _FanEntityFeature.SET_SPEED
        | _FanEntityFeature.PRESET_MODE
    )

    # 100% maps to the highest gear speed (High), not Turbo.
    await entity.async_set_percentage(100)
    assert coordinator.fan_mode_commands[-1] == "High"
    assert entity.percentage == 100
    assert entity.preset_mode == "Manual"

    await entity.async_set_preset_mode("Sleep")
    assert coordinator.fan_mode_commands[-1] == "Sleep"
    assert entity.percentage is None
    assert entity.preset_mode == "Sleep"

    await entity.async_set_preset_mode("Auto")
    assert coordinator.fan_mode_commands[-1] == "Auto"
    assert entity.percentage is None
    assert entity.preset_mode == "Auto"

    # Selecting Manual restores the last gear speed (High).
    await entity.async_set_preset_mode("Manual")
    assert coordinator.fan_mode_commands[-1] == "High"
    assert entity.preset_mode == "Manual"
    assert entity.percentage == 100

    await entity.async_set_percentage(0)
    assert coordinator.power_commands[-1] is False
