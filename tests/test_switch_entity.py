import importlib
import sys
from types import ModuleType, SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE


MODULE_NAME = "custom_components.govee_ble_air_purifier.switch"


class _CoordinatorEntity:
    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self.hass = None
        self._remove_callbacks = []
        self.state_writes = 0

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, callback) -> None:
        self._remove_callbacks.append(callback)

    def async_write_ha_state(self) -> None:
        self.state_writes += 1


class _SwitchEntity:
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
        self.fan_mode_commands: list[str] = []
        self.fail_modes: set[str] = set()

    async def async_set_fan_mode(self, mode: str) -> None:
        self.fan_mode_commands.append(mode)
        if mode in self.fail_modes:
            raise RuntimeError(f"failed to set {mode}")


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
        self, *, restored_speed: int | None = None, restoring: bool = False
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
    homeassistant_module = ModuleType("homeassistant")
    components_module = ModuleType("homeassistant.components")
    switch_module = ModuleType("homeassistant.components.switch")
    config_entries_module = ModuleType("homeassistant.config_entries")
    const_module = ModuleType("homeassistant.const")
    core_module = ModuleType("homeassistant.core")
    exceptions_module = ModuleType("homeassistant.exceptions")
    helpers_module = ModuleType("homeassistant.helpers")
    device_registry_module = ModuleType("homeassistant.helpers.device_registry")
    entity_registry_module = ModuleType("homeassistant.helpers.entity_registry")
    entity_platform_module = ModuleType("homeassistant.helpers.entity_platform")
    restore_state_module = ModuleType("homeassistant.helpers.restore_state")
    update_coordinator_module = ModuleType("homeassistant.helpers.update_coordinator")

    switch_module.SwitchEntity = _SwitchEntity
    config_entries_module.ConfigEntry = object
    const_module.STATE_ON = "on"
    core_module.HomeAssistant = object
    exceptions_module.HomeAssistantError = _HomeAssistantError
    device_registry_module.DeviceInfo = _DeviceInfo
    entity_platform_module.AddEntitiesCallback = object
    restore_state_module.RestoreEntity = _RestoreEntity
    restore_state_module.async_get = lambda hass: hass.restore_state
    entity_registry_module.async_get = lambda hass: hass.entity_registry
    update_coordinator_module.CoordinatorEntity = _CoordinatorEntity

    homeassistant_module.components = components_module
    homeassistant_module.config_entries = config_entries_module
    homeassistant_module.const = const_module
    homeassistant_module.core = core_module
    homeassistant_module.exceptions = exceptions_module
    homeassistant_module.helpers = helpers_module
    components_module.switch = switch_module
    helpers_module.device_registry = device_registry_module
    helpers_module.entity_registry = entity_registry_module
    helpers_module.entity_platform = entity_platform_module
    helpers_module.restore_state = restore_state_module
    helpers_module.update_coordinator = update_coordinator_module

    modules = {
        "homeassistant": homeassistant_module,
        "homeassistant.components": components_module,
        "homeassistant.components.switch": switch_module,
        "homeassistant.config_entries": config_entries_module,
        "homeassistant.const": const_module,
        "homeassistant.core": core_module,
        "homeassistant.exceptions": exceptions_module,
        "homeassistant.helpers": helpers_module,
        "homeassistant.helpers.device_registry": device_registry_module,
        "homeassistant.helpers.entity_registry": entity_registry_module,
        "homeassistant.helpers.entity_platform": entity_platform_module,
        "homeassistant.helpers.restore_state": restore_state_module,
        "homeassistant.helpers.update_coordinator": update_coordinator_module,
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)

    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


@pytest.mark.asyncio
async def test_switch_setup_creates_custom_auto_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom"},
        runtime_data=SimpleNamespace(
            coordinator=coordinator,
            controller=controller,
        ),
    )
    added_entities = []

    await switch.async_setup_entry(object(), entry, added_entities.extend)

    assert len(added_entities) == 1
    entity = added_entities[0]
    assert isinstance(entity, switch.GoveeCustomAutoSwitch)
    assert entity._attr_unique_id == "aabbccddeeff_custom_auto"
    assert entity._attr_translation_key == "custom_auto"


@pytest.mark.asyncio
async def test_switch_activates_custom_auto_and_hands_off_to_hardware_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    await entity.async_added_to_hass()

    assert entity.is_on is False
    await entity.async_turn_on()
    assert entity.is_on is True
    assert controller.activations == [(None, False)]

    await entity.async_turn_off()
    assert entity.is_on is False
    assert controller.handoffs == 1
    assert coordinator.fan_mode_commands == ["Auto"]
    assert entity.state_writes == 2


@pytest.mark.asyncio
async def test_switch_restores_custom_auto_when_fan_entity_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    entity._test_last_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": True, "custom_auto_speed": 60},
    )

    await entity.async_added_to_hass()

    assert controller.activations == [(60, True)]
    assert entity.extra_state_attributes == {
        "custom_auto_active": True,
        "custom_auto_speed": 60,
    }


@pytest.mark.asyncio
async def test_switch_restores_from_pre_attribute_on_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    entity._test_last_state = SimpleNamespace(state="on", attributes={})

    await entity.async_added_to_hass()

    assert controller.activations == [(None, True)]


@pytest.mark.asyncio
async def test_switch_migrates_restore_state_from_legacy_fan_entity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    legacy_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": True, "custom_auto_speed": 60},
    )
    entity.hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: (
                "fan.bedroom" if unique_id == "aabbccddeeff_fan" else None
            )
        ),
        restore_state=SimpleNamespace(
            last_states={
                "fan.bedroom": SimpleNamespace(state=legacy_state),
            }
        ),
    )

    await entity.async_added_to_hass()

    assert controller.activations == [(60, True)]


@pytest.mark.asyncio
async def test_switch_does_not_migrate_legacy_fan_power_as_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    legacy_state = SimpleNamespace(
        state="on",
        attributes={"custom_auto_active": False, "custom_auto_speed": None},
    )
    entity.hass = SimpleNamespace(
        entity_registry=SimpleNamespace(
            async_get_entity_id=lambda domain, platform, unique_id: "fan.bedroom"
        ),
        restore_state=SimpleNamespace(
            last_states={
                "fan.bedroom": SimpleNamespace(state=legacy_state),
            }
        ),
    )

    await entity.async_added_to_hass()

    assert controller.activations == []


@pytest.mark.asyncio
async def test_switch_normal_startup_does_not_activate_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)

    await entity.async_added_to_hass()

    assert controller.activations == []
    assert entity.extra_state_attributes == {
        "custom_auto_active": False,
        "custom_auto_speed": None,
    }


@pytest.mark.asyncio
async def test_failed_hardware_auto_handoff_reactivates_custom_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    controller.active = True
    controller.current_speed = 40
    coordinator.fail_modes.add("Auto")
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)

    with pytest.raises(_HomeAssistantError, match="Failed to disable Custom Auto"):
        await entity.async_turn_off()

    assert controller.active is True
    assert controller.handoffs == 1
    assert controller.activations == []


@pytest.mark.asyncio
async def test_switch_reflects_external_controller_deactivation_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    switch = _import_switch(monkeypatch)
    coordinator = _FakeCoordinator()
    controller = _FakeController()
    entry = SimpleNamespace(unique_id="aabbccddeeff", data={"name": "Bedroom"})
    entity = switch.GoveeCustomAutoSwitch(coordinator, entry, controller)
    await entity.async_added_to_hass()
    await entity.async_turn_on()

    await controller.async_deactivate()

    assert entity.is_on is False
    assert entity.state_writes == 2
    assert len(controller.listeners) == 1
    entity._remove_callbacks[0]()
    assert controller.listeners == []
