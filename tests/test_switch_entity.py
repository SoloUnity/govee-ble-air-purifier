import importlib
import sys
from dataclasses import replace
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


MODULE_NAME = "custom_components.govee_ble_air_purifier.switch"


class _SwitchEntity:
    pass


class _FakeCoordinator:
    profile = H7124_PROFILE

    def __init__(self) -> None:
        self.data = PurifierState(is_on=True, fan_mode="Low")
        self.poll_revision = 0
        self.fan_mode_commands: list[str] = []
        self.fail_modes: set[str] = set()
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    async def async_set_fan_mode(self, mode: str) -> None:
        self.fan_mode_commands.append(mode)
        if mode in self.fail_modes:
            raise RuntimeError(f"failed to set {mode}")
        self.data = PurifierState(is_on=True, fan_mode=mode)
        for listener in list(self.listeners):
            listener()


class _FakeController:
    def __init__(self) -> None:
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
        if restored_speed is not None:
            self.current_speed = restored_speed
        self.active = True
        self.notify()

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


def _import_switch(monkeypatch: pytest.MonkeyPatch):
    install_modules(
        monkeypatch,
        {
            "homeassistant.components.switch": {"SwitchEntity": _SwitchEntity},
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.core": {"HomeAssistant": object},
            "homeassistant.exceptions": {"HomeAssistantError": _HomeAssistantError},
            "homeassistant.helpers.device_registry": {"DeviceInfo": _DeviceInfo},
            "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
            "homeassistant.helpers.restore_state": {"RestoreEntity": _RestoreEntity},
            "homeassistant.helpers.update_coordinator": {
                "CoordinatorEntity": _CoordinatorEntity
            },
        },
    )

    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


def _auto_resume(
    coordinator: _FakeCoordinator, controller: _FakeController
) -> AutoResumeManager:
    return AutoResumeManager(None, coordinator, controller)


@pytest.mark.asyncio
async def test_switch_setup_creates_custom_auto_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            controller=controller,
            auto_resume=auto_resume,
        ),
    )
    added_entities = []

    await switch.async_setup_entry(object(), entry, added_entities.extend)

    assert len(added_entities) == 1
    entity = added_entities[0]
    assert isinstance(entity, switch.GoveeCustomAutoSwitch)
    assert entity._attr_unique_id == "aabbccddeeff_custom_auto"
    assert entity._attr_translation_key == "custom_auto"
    assert switch.ATTR_CUSTOM_AUTO_ACTIVE == "custom_auto_active"
    assert switch.ATTR_CUSTOM_AUTO_SPEED == "custom_auto_speed"


@pytest.mark.asyncio
async def test_switch_setup_skips_profile_without_custom_auto_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.profile = replace(
        H7124_PROFILE,
        fan_mode_commands={"Low": H7124_PROFILE.fan_mode_commands["Low"]},
    )
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            controller=_FakeController(),
            auto_resume=SimpleNamespace(),
        ),
    )
    added_entities = []

    await switch.async_setup_entry(object(), entry, added_entities.extend)

    assert added_entities == []


@pytest.mark.asyncio
async def test_switch_activates_custom_auto_and_hands_off_to_hardware_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(
        coordinator, entry, controller, auto_resume
    )
    await entity.async_added_to_hass()

    assert entity.is_on is False
    await entity.async_turn_on()
    assert entity.is_on is True
    assert controller.activations == [(None, False)]

    await entity.async_turn_off()
    assert entity.is_on is False
    assert controller.handoffs == 1
    assert coordinator.fan_mode_commands == ["Auto"]
    assert auto_resume.state.mode == AUTO_MODE_HARDWARE


@pytest.mark.asyncio
async def test_switch_remains_on_while_custom_auto_is_suspended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    coordinator.data = PurifierState(is_on=False, fan_mode=None)
    controller = _FakeController()
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(
        coordinator, entry, controller, auto_resume
    )
    await auto_resume.async_restore(
        AUTO_MODE_CUSTOM, suspended=True, custom_speed=60
    )

    await entity.async_added_to_hass()

    assert entity.is_on is True
    assert entity.extra_state_attributes == {
        "custom_auto_active": False,
        "custom_auto_speed": 60,
        "auto_resume_mode": AUTO_MODE_CUSTOM,
        "auto_resume_suspended": True,
        "auto_resume_custom_speed": 60,
    }

    await entity.async_turn_off()

    assert entity.is_on is False
    assert auto_resume.state.mode == AUTO_MODE_HARDWARE
    assert auto_resume.state.suspended is True
    assert coordinator.fan_mode_commands == []


@pytest.mark.asyncio
async def test_failed_hardware_auto_handoff_reactivates_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    auto_resume = _auto_resume(coordinator, controller)
    await auto_resume.async_enable_custom_auto()
    controller.current_speed = 40
    coordinator.fail_modes.add("Auto")
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(
        coordinator, entry, controller, auto_resume
    )

    with pytest.raises(_HomeAssistantError, match="Failed to disable Custom Auto"):
        await entity.async_turn_off()

    assert controller.active is True
    assert controller.handoffs == 1
    assert auto_resume.state.mode == AUTO_MODE_CUSTOM


@pytest.mark.asyncio
async def test_switch_reflects_resume_state_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    auto_resume = _auto_resume(coordinator, controller)
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(
        coordinator, entry, controller, auto_resume
    )
    await entity.async_added_to_hass()
    await entity.async_turn_on()

    await controller.async_deactivate()

    assert entity.is_on is True
    assert len(controller.listeners) == 1
    entity._remove_callbacks[0]()
    assert controller.listeners == []
    entity._remove_callbacks[1]()
    writes = entity.state_writes
    await auto_resume.async_set_hardware_auto()
    assert entity.state_writes == writes
