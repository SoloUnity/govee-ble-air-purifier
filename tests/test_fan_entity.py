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


class _LegacyFanEntityFeature(IntFlag):
    SET_SPEED = 1
    PRESET_MODE = 8


class _CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._remove_callbacks = []
        self.state_writes = 0

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, callback) -> None:
        self._remove_callbacks.append(callback)

    def async_write_ha_state(self) -> None:
        self.state_writes += 1


class _FanEntity:
    pass


class _RestoreEntity:
    async def async_get_last_state(self):
        return getattr(self, "_test_last_state", None)


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
        self.fail_modes: set[str] = set()
        self.fail_power = False

    async def async_set_power(self, is_on: bool) -> None:
        self.power_commands.append(is_on)
        if self.fail_power:
            raise RuntimeError("failed to set power")
        self.data = GoveeData(
            is_on=is_on,
            pm25=self.data.pm25 if self.data else None,
            filter_life=self.data.filter_life if self.data else None,
            fan_mode=self.data.fan_mode if is_on and self.data else None,
        )

    async def async_set_fan_mode(self, mode: str) -> None:
        self.fan_mode_commands.append(mode)
        if mode in self.fail_modes:
            raise RuntimeError(f"failed to set {mode}")
        self.data = GoveeData(is_on=True, pm25=7, filter_life=93, fan_mode=mode)


class _FakeController:
    def __init__(self, coordinator: _FakeCoordinator) -> None:
        self.coordinator = coordinator
        self.active = False
        self.current_speed = 80
        self.activations: list[tuple[int | None, bool]] = []
        self.deactivations = 0
        self.handoffs = 0
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)

        def remove() -> None:
            self.listeners.remove(listener)

        return remove

    def notify(self) -> None:
        for listener in list(self.listeners):
            listener()

    async def async_activate(
        self, *, restored_speed: int | None = None, restoring: bool = False
    ) -> None:
        self.activations.append((restored_speed, restoring))
        self.active = True
        self.notify()
        if restored_speed is not None:
            self.current_speed = restored_speed
        await self.coordinator.async_set_fan_mode(
            {20: "Sleep", 40: "Low", 60: "Medium", 80: "High", 100: "Turbo"}[
                self.current_speed
            ]
        )

    async def async_deactivate(self) -> None:
        self.deactivations += 1
        self.active = False
        self.notify()

    async def async_handoff(self, command) -> None:
        self.handoffs += 1
        await command()
        self.active = False
        self.deactivations += 1
        self.notify()


def _install_homeassistant_modules(
    monkeypatch: pytest.MonkeyPatch,
    fan_features: type[IntFlag] = _FanEntityFeature,
) -> None:
    homeassistant_module = ModuleType("homeassistant")
    components_module = ModuleType("homeassistant.components")
    fan_module = ModuleType("homeassistant.components.fan")
    config_entries_module = ModuleType("homeassistant.config_entries")
    core_module = ModuleType("homeassistant.core")
    exceptions_module = ModuleType("homeassistant.exceptions")
    helpers_module = ModuleType("homeassistant.helpers")
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")
    restore_state_module = ModuleType("homeassistant.helpers.restore_state")
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

    fan_module.FanEntity = _FanEntity
    fan_module.FanEntityFeature = fan_features
    config_entries_module.ConfigEntry = object
    core_module.HomeAssistant = object
    exceptions_module.HomeAssistantError = _HomeAssistantError
    device_registry_module.DeviceInfo = _DeviceInfo
    entity_platform_module.AddEntitiesCallback = object
    restore_state_module.RestoreEntity = _RestoreEntity
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
    helpers_module.restore_state = restore_state_module
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
        "homeassistant.helpers.restore_state": restore_state_module,
        "homeassistant.helpers.update_coordinator": update_coordinator_module,
        "homeassistant.util": util_module,
        "homeassistant.util.percentage": percentage_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)


def _import_fan(
    monkeypatch: pytest.MonkeyPatch,
    fan_features: type[IntFlag] = _FanEntityFeature,
):
    _install_homeassistant_modules(monkeypatch, fan_features)
    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


def test_loaded_platforms_match_cloud_style_control_layout() -> None:
    from custom_components.govee_ble_air_purifier.const import PLATFORMS

    assert PLATFORMS == ["fan", "sensor", "switch"]


def test_fan_import_supports_home_assistant_before_turn_feature_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch, _LegacyFanEntityFeature)

    assert fan.GoveeAirPurifierFan._attr_supported_features == (
        _LegacyFanEntityFeature.SET_SPEED | _LegacyFanEntityFeature.PRESET_MODE
    )


@pytest.mark.asyncio
async def test_fan_setup_creates_one_air_purifier_fan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(coordinator=coordinator, controller=None),
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
    assert entity.percentage == 40
    assert entity.preset_mode == "Manual"
    assert entity._attr_preset_modes == ["Manual", "Auto"]
    assert entity._attr_speed_count == 5
    assert entity._manual_speeds == ["Sleep", "Low", "Medium", "High", "Turbo"]
    assert entity._attr_supported_features == (
        _FanEntityFeature.TURN_ON
        | _FanEntityFeature.TURN_OFF
        | _FanEntityFeature.SET_SPEED
        | _FanEntityFeature.PRESET_MODE
    )

    await entity.async_set_percentage(100)
    assert coordinator.fan_mode_commands[-1] == "Turbo"
    assert entity.percentage == 100
    assert entity.preset_mode == "Manual"

    await entity.async_set_preset_mode("Auto")
    assert coordinator.fan_mode_commands[-1] == "Auto"
    assert entity.percentage is None
    assert entity.preset_mode == "Auto"

    await entity.async_set_preset_mode("Manual")
    assert coordinator.fan_mode_commands[-1] == "Turbo"

    await entity.async_set_percentage(0)
    assert coordinator.power_commands[-1] is False


@pytest.mark.asyncio
async def test_custom_auto_reports_auto_with_underlying_percentage_and_manual_disables_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)

    await controller.async_activate()
    assert entity.preset_mode == "Auto"
    assert entity.percentage == 80

    await entity.async_set_percentage(100)
    assert controller.handoffs == 1
    assert coordinator.fan_mode_commands[-1] == "Turbo"
    assert entity.preset_mode == "Manual"

    await controller.async_activate()
    await entity.async_set_preset_mode("Manual")
    assert controller.handoffs == 2
    assert entity.preset_mode == "Manual"

    await controller.async_activate()
    await entity.async_turn_off()
    assert controller.handoffs == 3
    assert coordinator.power_commands[-1] is False


@pytest.mark.asyncio
async def test_auto_preset_deactivates_custom_auto_and_uses_hardware_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)

    await controller.async_activate()
    await entity.async_set_preset_mode("Auto")

    assert controller.handoffs == 1
    assert coordinator.fan_mode_commands[-1] == "Auto"
    assert entity.preset_mode == "Auto"
    assert entity.percentage is None


@pytest.mark.asyncio
async def test_manual_speed_is_preserved_across_hardware_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry)

    await entity.async_set_preset_mode("Auto")
    await entity.async_set_preset_mode("Manual")

    assert coordinator.fan_mode_commands == ["Auto", "Low"]


@pytest.mark.asyncio
async def test_fan_is_not_a_custom_auto_restoration_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    last_state = SimpleNamespace(
        attributes={"custom_auto_active": True, "custom_auto_speed": 60}
    )

    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)
    entity._test_last_state = last_state
    await entity.async_added_to_hass()

    assert controller.activations == []


@pytest.mark.asyncio
async def test_failed_hardware_mode_handoff_reactivates_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)
    await controller.async_activate(restored_speed=40)
    coordinator.fail_modes.add("Auto")

    with pytest.raises(_HomeAssistantError, match="Failed to set purifier preset"):
        await entity.async_set_preset_mode("Auto")

    assert controller.active is True
    assert controller.handoffs == 1
    assert controller.activations == [(40, False)]
    assert coordinator.fan_mode_commands[-1] == "Auto"


@pytest.mark.asyncio
async def test_failed_power_off_handoff_keeps_custom_auto_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)
    await controller.async_activate(restored_speed=40)
    coordinator.fail_power = True

    with pytest.raises(_HomeAssistantError, match="Failed to turn purifier off"):
        await entity.async_turn_off()

    assert controller.active is True
    assert controller.handoffs == 1


@pytest.mark.asyncio
async def test_fan_writes_controller_state_and_removes_listener_on_entity_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller)

    await entity.async_added_to_hass()
    controller.active = True
    controller.notify()

    assert entity.state_writes == 1
    assert len(controller.listeners) == 1
    entity._remove_callbacks[0]()
    assert controller.listeners == []
