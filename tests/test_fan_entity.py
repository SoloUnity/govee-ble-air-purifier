import asyncio
import importlib
import math
import sys
from enum import IntFlag
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.auto_resume import (
    AUTO_MODE_CUSTOM,
    AUTO_MODE_HARDWARE,
    AutoResumeManager,
)
from custom_components.govee_ble_air_purifier.models import PurifierState
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE
from tests.helpers.ha_stubs import (
    CoordinatorEntity as _CoordinatorEntity,
    DeviceInfo as _DeviceInfo,
    HomeAssistantError as _HomeAssistantError,
    RestoreEntity as _RestoreEntity,
    install_modules,
)


MODULE_NAME = "custom_components.govee_ble_air_purifier.fan"


class _FanEntityFeature(IntFlag):
    TURN_ON = 1
    TURN_OFF = 2
    SET_SPEED = 4
    PRESET_MODE = 8


class _LegacyFanEntityFeature(IntFlag):
    SET_SPEED = 1
    PRESET_MODE = 8


class _FanEntity:
    pass


class _FakeCoordinator:
    profile = H7124_PROFILE

    def __init__(self) -> None:
        self.data = PurifierState(is_on=True, pm25=7, filter_life=93, fan_mode="Low")
        self.poll_revision = 0
        self.power_commands: list[bool] = []
        self.fan_mode_commands: list[str] = []
        self.fail_modes: set[str] = set()
        self.fail_power = False
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self.listeners):
            listener()

    async def async_set_power(self, is_on: bool) -> None:
        self.power_commands.append(is_on)
        if self.fail_power:
            raise RuntimeError("failed to set power")
        self.data = PurifierState(
            is_on=is_on,
            pm25=self.data.pm25 if self.data else None,
            filter_life=self.data.filter_life if self.data else None,
            fan_mode=self.data.fan_mode if is_on and self.data else None,
        )
        self._notify()

    async def async_set_fan_mode(self, mode: str) -> None:
        self.fan_mode_commands.append(mode)
        if mode in self.fail_modes:
            raise RuntimeError(f"failed to set {mode}")
        self.data = PurifierState(is_on=True, pm25=7, filter_life=93, fan_mode=mode)
        self._notify()


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
        self,
        *,
        restored_speed: int | None = None,
        restoring: bool = False,
        force: bool = False,
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
    def ordered_list_item_to_percentage(options: list[str], item: str) -> int:
        return round(((options.index(item) + 1) * 100) / len(options))

    def percentage_to_ordered_list_item(options: list[str], percentage: int) -> str:
        index = min(
            len(options) - 1,
            max(0, math.ceil((percentage * len(options)) / 100) - 1),
        )
        return options[index]

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.fan": {
                "FanEntity": _FanEntity,
                "FanEntityFeature": fan_features,
            },
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.const": {"STATE_ON": "on"},
            "homeassistant.core": {"HomeAssistant": object},
            "homeassistant.exceptions": {"HomeAssistantError": _HomeAssistantError},
            "homeassistant.helpers.device_registry": {
                "CONNECTION_BLUETOOTH": "bluetooth",
                "DeviceInfo": _DeviceInfo,
            },
            "homeassistant.helpers.entity_registry": {
                "async_get": lambda hass: hass.entity_registry
            },
            "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
            "homeassistant.helpers.restore_state": {
                "RestoreEntity": _RestoreEntity,
                "async_get": lambda hass: hass.restore_state,
            },
            "homeassistant.helpers.update_coordinator": {
                "CoordinatorEntity": _CoordinatorEntity
            },
            "homeassistant.util.percentage": {
                "ordered_list_item_to_percentage": ordered_list_item_to_percentage,
                "percentage_to_ordered_list_item": percentage_to_ordered_list_item,
            },
        },
    )


def _import_fan(
    monkeypatch: pytest.MonkeyPatch,
    fan_features: type[IntFlag] = _FanEntityFeature,
):
    _install_homeassistant_modules(monkeypatch, fan_features)
    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


def _auto_resume(
    coordinator: _FakeCoordinator, controller: _FakeController, hass=None
) -> AutoResumeManager:
    return AutoResumeManager(hass, coordinator, controller)


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
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            controller=controller,
            auto_resume=auto_resume,
        ),
    )
    added_entities = []

    await fan.async_setup_entry(object(), entry, added_entities.extend)

    assert len(added_entities) == 1
    assert isinstance(added_entities[0], fan.GoveeAirPurifierFan)
    assert added_entities[0]._attr_unique_id == "aabbccddeeff_fan"
    assert added_entities[0]._attr_name is None


@pytest.mark.asyncio
async def test_fan_entity_maps_power_speed_and_presets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

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
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

    await auto_resume.async_enable_custom_auto()
    assert entity.preset_mode == "Auto"
    assert entity.percentage == 80

    await entity.async_set_percentage(100)
    assert controller.handoffs == 1
    assert coordinator.fan_mode_commands[-1] == "Turbo"
    assert entity.preset_mode == "Manual"

    await auto_resume.async_enable_custom_auto()
    await entity.async_set_preset_mode("Manual")
    assert controller.handoffs == 2
    assert entity.preset_mode == "Manual"

    await auto_resume.async_enable_custom_auto()
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
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

    await auto_resume.async_enable_custom_auto()
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
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

    await entity.async_set_preset_mode("Auto")
    await entity.async_set_preset_mode("Manual")

    assert coordinator.fan_mode_commands == ["Auto", "Low"]


@pytest.mark.asyncio
async def test_fan_migrates_legacy_custom_auto_restore_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    last_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": True, "custom_auto_speed": 60}
    )
    hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "fan.bedroom" if domain == "fan" else None
            )
        ),
        restore_state=SimpleNamespace(
            last_states={"fan.bedroom": SimpleNamespace(state=last_state)}
        ),
    )
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller, hass)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)
    await auto_resume.async_restore_from_hass(entry.unique_id)
    await entity.async_added_to_hass()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert auto_resume.state.mode == AUTO_MODE_CUSTOM
    assert controller.activations == [(60, True)]


@pytest.mark.asyncio
async def test_fan_restores_suspended_custom_auto_without_powering_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.data = PurifierState(
        is_on=False, pm25=7, filter_life=93, fan_mode=None
    )
    controller = _FakeController(coordinator)
    restored_state = SimpleNamespace(
        state="off",
        attributes={
            "auto_resume_mode": AUTO_MODE_CUSTOM,
            "auto_resume_suspended": True,
            "auto_resume_custom_speed": 40,
        },
    )
    hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "fan.bedroom" if domain == "fan" else None
            )
        ),
        restore_state=SimpleNamespace(
            last_states={"fan.bedroom": SimpleNamespace(state=restored_state)}
        ),
    )
    auto_resume = _auto_resume(coordinator, controller, hass)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

    await auto_resume.async_restore_from_hass(entry.unique_id)
    await entity.async_added_to_hass()

    assert controller.activations == []
    assert coordinator.power_commands == []
    assert entity.preset_mode == "Auto"
    assert entity.extra_state_attributes == {
        "auto_resume_mode": AUTO_MODE_CUSTOM,
        "auto_resume_suspended": True,
        "auto_resume_custom_speed": 40,
    }


@pytest.mark.asyncio
async def test_fan_migrates_existing_custom_auto_switch_restore_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.data = PurifierState(
        is_on=False, pm25=7, filter_life=93, fan_mode=None
    )
    controller = _FakeController(coordinator)
    switch_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": True, "custom_auto_speed": 60},
    )
    hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "switch.bedroom_custom_auto" if domain == "switch" else None
            )
        ),
        restore_state=SimpleNamespace(
            last_states={
                "switch.bedroom_custom_auto": SimpleNamespace(state=switch_state)
            }
        ),
    )
    auto_resume = _auto_resume(coordinator, controller, hass)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})

    await auto_resume.async_restore_from_hass(entry.unique_id)

    assert auto_resume.state.mode == AUTO_MODE_CUSTOM
    assert auto_resume.state.suspended is True
    assert auto_resume.state.custom_speed == 60
    assert controller.activations == []


@pytest.mark.asyncio
async def test_new_fan_restore_schema_prevents_stale_switch_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    fan_state = SimpleNamespace(
        state="on",
        attributes={
            "auto_resume_mode": None,
            "auto_resume_suspended": False,
            "auto_resume_custom_speed": None,
        },
    )
    switch_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": True},
    )
    hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "fan.bedroom"
                if domain == "fan"
                else "switch.bedroom_custom_auto"
            )
        ),
        restore_state=SimpleNamespace(
            last_states={
                "fan.bedroom": SimpleNamespace(state=fan_state),
                "switch.bedroom_custom_auto": SimpleNamespace(
                    state=switch_state
                )
            }
        ),
    )
    auto_resume = _auto_resume(coordinator, controller, hass)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})

    await auto_resume.async_restore_from_hass(entry.unique_id)

    assert auto_resume.state.mode is None
    assert controller.activations == []


@pytest.mark.asyncio
async def test_newest_restore_replica_wins_when_fan_was_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    fan_state = SimpleNamespace(
        state="on",
        last_updated=SimpleNamespace(timestamp=lambda: 1.0),
        attributes={
            "auto_resume_mode": AUTO_MODE_HARDWARE,
            "auto_resume_suspended": False,
            "auto_resume_custom_speed": None,
        },
    )
    switch_state = SimpleNamespace(
        state="on",
        last_updated=SimpleNamespace(timestamp=lambda: 2.0),
        attributes={
            "auto_resume_mode": AUTO_MODE_CUSTOM,
            "auto_resume_suspended": False,
            "auto_resume_custom_speed": 60,
        },
    )
    hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "fan.bedroom"
                if domain == "fan"
                else "switch.bedroom_custom_auto"
            )
        ),
        restore_state=SimpleNamespace(
            last_states={
                "fan.bedroom": SimpleNamespace(state=fan_state),
                "switch.bedroom_custom_auto": SimpleNamespace(
                    state=switch_state
                ),
            }
        ),
    )
    auto_resume = _auto_resume(coordinator, controller, hass)

    await auto_resume.async_restore_from_hass("aabbccddeeff")
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert auto_resume.state.mode == AUTO_MODE_CUSTOM
    assert auto_resume.state.custom_speed == 60


@pytest.mark.asyncio
async def test_failed_hardware_mode_handoff_reactivates_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)
    await auto_resume.async_enable_custom_auto()
    controller.current_speed = 40
    coordinator.fail_modes.add("Auto")

    with pytest.raises(_HomeAssistantError, match="Failed to set purifier preset"):
        await entity.async_set_preset_mode("Auto")

    assert controller.active is True
    assert controller.handoffs == 1
    assert controller.activations == [(None, False)]
    assert coordinator.fan_mode_commands[-1] == "Auto"


@pytest.mark.asyncio
async def test_failed_power_off_handoff_keeps_custom_auto_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)
    await auto_resume.async_enable_custom_auto()
    controller.current_speed = 40
    coordinator.fail_power = True

    with pytest.raises(_HomeAssistantError, match="Failed to turn purifier off"):
        await entity.async_turn_off()

    assert controller.active is True
    assert controller.handoffs == 1


@pytest.mark.asyncio
async def test_fan_writes_resume_state_and_removes_listener_on_entity_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fan = _import_fan(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController(coordinator)
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = fan.GoveeAirPurifierFan(coordinator, entry, controller, auto_resume)

    await entity.async_added_to_hass()
    await auto_resume.async_set_hardware_auto()

    assert entity.state_writes == 1
    assert auto_resume.state.mode == AUTO_MODE_HARDWARE
    entity._remove_callbacks[0]()
    await auto_resume.async_set_manual_mode("Low")
    assert entity.state_writes == 1
