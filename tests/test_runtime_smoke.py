"""Smoke tests that run only with a real Home Assistant installation."""

from importlib import import_module
from inspect import signature
from types import SimpleNamespace

import pytest

homeassistant = pytest.importorskip(
    "homeassistant",
    reason="real Home Assistant is required for runtime smoke tests",
)
if getattr(homeassistant, "_FAST_TEST_STUB", False):
    pytest.skip(
        "real Home Assistant is required for runtime smoke tests",
        allow_module_level=True,
    )

pytestmark = pytest.mark.runtime_ha


INTEGRATION_PACKAGE = "custom_components.govee_ble_air_purifier"
RUNTIME_MODULES = (
    "bluetooth.client",
    "bluetooth.framing",
    "bluetooth.transport",
    "config_flow",
    "coordinator",
    "custom_auto.config",
    "custom_auto.controller",
    "custom_auto.policy",
    "diagnostics",
    "entity",
    "fan",
    "profiles",
    "sensor",
    "switch",
)


def test_runtime_modules_import_with_real_home_assistant() -> None:
    """Import all HA-facing modules without the lightweight suite's stubs."""
    homeassistant = import_module("homeassistant")

    assert homeassistant.__spec__ is not None
    assert homeassistant.__spec__.submodule_search_locations is not None

    for module_name in RUNTIME_MODULES:
        import_module(f"{INTEGRATION_PACKAGE}.{module_name}")

    from homeassistant.config_entries import ConfigFlow
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from custom_components.govee_ble_air_purifier.config_flow import (
        GoveeBleAirPurifierConfigFlow,
    )
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    assert issubclass(GoveeBleAirPurifierConfigFlow, ConfigFlow)
    assert issubclass(GoveeCoordinator, DataUpdateCoordinator)
    assert "options" in signature(ConfigFlow.async_create_entry).parameters


def test_family_fallback_profile_loads_with_real_home_assistant() -> None:
    from custom_components.govee_ble_air_purifier.profiles import get_profile

    profile = get_profile("h712c")

    assert profile.key == "h712c"
    assert profile.model == "H712C"
    assert profile.local_name_prefixes == ("GVH712C",)


def test_bluetooth_recovery_uses_supported_home_assistant_apis() -> None:
    from homeassistant.components import bluetooth
    from homeassistant.core import HomeAssistant

    process_parameters = signature(bluetooth.async_process_advertisements).parameters

    assert tuple(process_parameters) == (
        "hass",
        "callback",
        "match_dict",
        "mode",
        "timeout",
    )
    assert callable(bluetooth.async_last_service_info)
    assert callable(bluetooth.async_scanner_devices_by_address)
    assert bluetooth.BluetoothScanningMode.ACTIVE is not None
    clear_history = getattr(bluetooth, "async_clear_advertisement_history", None)
    request_active_scan = getattr(bluetooth, "async_request_active_scan", None)
    assert clear_history is None or callable(clear_history)
    assert request_active_scan is None or callable(request_active_scan)
    assert callable(HomeAssistant.async_create_background_task)


@pytest.mark.asyncio
async def test_platform_setup_uses_real_home_assistant_entities() -> None:
    """Construct each configured platform's entities through its setup hook."""
    from homeassistant.components.fan import FanEntity
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.components.switch import SwitchEntity

    from custom_components.govee_ble_air_purifier.models import PurifierState
    from custom_components.govee_ble_air_purifier.fan import (
        async_setup_entry as setup_fan,
    )
    from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE
    from custom_components.govee_ble_air_purifier.sensor import (
        async_setup_entry as setup_sensor,
    )
    from custom_components.govee_ble_air_purifier.switch import (
        async_setup_entry as setup_switch,
    )

    coordinator = SimpleNamespace(
        async_add_listener=lambda listener: lambda: None,
        data=PurifierState(is_on=True, pm25=7, filter_life=95, fan_mode="Low"),
        last_update_success=True,
        profile=H7124_PROFILE,
    )
    controller = SimpleNamespace(
        active=False,
        async_add_listener=lambda listener: lambda: None,
    )
    auto_resume = SimpleNamespace(
        async_add_listener=lambda listener: lambda: None,
    )
    entry = SimpleNamespace(
        data={"name": "Runtime smoke purifier"},
        runtime_data=SimpleNamespace(
            controller=controller,
            coordinator=coordinator,
            auto_resume=auto_resume,
        ),
        unique_id="aabbccddeeff",
    )
    entities = []

    await setup_fan(None, entry, entities.extend)
    await setup_sensor(None, entry, entities.extend)
    await setup_switch(None, entry, entities.extend)

    assert sum(isinstance(entity, FanEntity) for entity in entities) == 1
    assert sum(isinstance(entity, SensorEntity) for entity in entities) == 2
    assert sum(isinstance(entity, SwitchEntity) for entity in entities) == 1


@pytest.mark.asyncio
async def test_real_config_flow_creates_complete_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise both setup steps through Home Assistant's real flow base class."""
    from homeassistant.components import bluetooth
    from homeassistant.data_entry_flow import FlowResultType

    from custom_components.govee_ble_air_purifier.config_flow import (
        GoveeBleAirPurifierConfigFlow,
    )
    from custom_components.govee_ble_air_purifier.custom_auto.config import (
        CUSTOM_AUTO_DEFAULTS,
    )

    address = "AA:BB:CC:DD:EE:FF"
    service_info = SimpleNamespace(
        address=address,
        name="GVH7124BEDROOM",
        rssi=-48,
        source="local",
    )
    monkeypatch.setattr(
        bluetooth,
        "async_discovered_service_info",
        lambda *args, **kwargs: (service_info,),
    )

    config_entries = SimpleNamespace(
        async_entries=lambda *args, **kwargs: (),
        async_entry_for_domain_unique_id=lambda *args, **kwargs: None,
        flow=SimpleNamespace(async_progress_by_handler=lambda *args, **kwargs: ()),
    )
    flow = GoveeBleAirPurifierConfigFlow()
    flow.hass = SimpleNamespace(config_entries=config_entries)
    flow.context = {"source": "user"}
    flow.flow_id = "runtime-smoke-flow"
    flow.handler = "govee_ble_air_purifier"
    first_result = await flow.async_step_user(
        {
            "discovered_device": address,
            "name": "Bedroom Purifier",
            "polling_interval": 20,
        }
    )

    assert first_result["type"] is FlowResultType.FORM
    assert first_result["step_id"] == "custom_auto"

    result = await flow.async_step_custom_auto(dict(CUSTOM_AUTO_DEFAULTS))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Bedroom Purifier"
    assert result["data"] == {
        "address": address,
        "name": "Bedroom Purifier",
        "profile": "h7124",
    }
    assert result["options"] == {
        "polling_interval": 20,
        **CUSTOM_AUTO_DEFAULTS,
    }


@pytest.mark.asyncio
async def test_integration_setup_and_unload_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Run production lifecycle hooks with controlled collaborators and no BLE."""
    from homeassistant.config_entries import ConfigEntry

    import custom_components.govee_ble_air_purifier as integration

    events: list[object] = []

    class FakeClient:
        def __init__(self, hass, address, *, profile, polling_interval_seconds) -> None:
            self.hass = hass
            self.address = address
            self.profile = profile
            self.polling_interval_seconds = polling_interval_seconds

    class FakeCoordinator:
        def __init__(self, hass, client, *, profile, polling_interval) -> None:
            self.hass = hass
            self.client = client
            self.profile = profile
            self.polling_interval = polling_interval

        async def async_config_entry_first_refresh(self) -> None:
            events.append("first_refresh")

        async def async_shutdown(self) -> None:
            events.append("shutdown")

    class FakeController:
        def __init__(self, hass, coordinator, config, *, config_entry) -> None:
            self.hass = hass
            self.coordinator = coordinator
            self.config = config
            self.config_entry = config_entry

        async def async_stop(self) -> None:
            events.append("controller_stop")

    class FakeAutoResume:
        def __init__(self, hass, coordinator, controller, *, config_entry) -> None:
            self.hass = hass
            self.coordinator = coordinator
            self.controller = controller
            self.config_entry = config_entry

        async def async_restore_from_hass(self, unique_id: str) -> None:
            events.append(("restore", unique_id))

        async def async_stop(self) -> None:
            events.append("auto_resume_stop")

    class FakeEntry:
        entry_id = "runtime-entry"
        unique_id = "aabbccddeeff"
        data = {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Bedroom Purifier",
            "profile": "h7124",
        }
        options = {"polling_interval": 20}
        runtime_data = None

        def __init__(self) -> None:
            self.update_listener = None
            self.unload_callbacks = []

        def add_update_listener(self, listener):
            self.update_listener = listener
            return lambda: None

        def async_on_unload(self, callback) -> None:
            self.unload_callbacks.append(callback)

    class FakeConfigEntries:
        async def async_forward_entry_setups(self, entry, platforms) -> None:
            events.append(("forward", tuple(platforms)))

        async def async_unload_platforms(self, entry, platforms) -> bool:
            events.append(("unload", tuple(platforms)))
            return True

        async def async_reload(self, entry_id) -> None:
            events.append(("reload", entry_id))

    assert ConfigEntry is not object
    monkeypatch.setattr(integration, "GoveeBleClient", FakeClient)
    monkeypatch.setattr(integration, "GoveeCoordinator", FakeCoordinator)
    monkeypatch.setattr(integration, "CustomAutoController", FakeController)
    monkeypatch.setattr(integration, "AutoResumeManager", FakeAutoResume)
    entry = FakeEntry()
    hass = SimpleNamespace(config_entries=FakeConfigEntries())

    assert await integration.async_setup_entry(hass, entry) is True
    assert entry.runtime_data.profile.key == "h7124"
    assert entry.runtime_data.coordinator.client.address == "AA:BB:CC:DD:EE:FF"
    assert entry.runtime_data.coordinator.client.polling_interval_seconds == 20
    assert entry.runtime_data.coordinator.polling_interval.total_seconds() == 20
    assert entry.runtime_data.controller.coordinator is entry.runtime_data.coordinator
    assert entry.update_listener is integration._async_update_listener
    assert len(entry.unload_callbacks) == 1
    assert events == [
        "first_refresh",
        ("restore", "aabbccddeeff"),
        ("forward", tuple(integration.PLATFORMS)),
    ]

    await entry.update_listener(hass, entry)
    assert events[-1] == ("reload", "runtime-entry")

    assert await integration.async_unload_entry(hass, entry) is True
    assert events[-4:] == [
        ("unload", tuple(integration.PLATFORMS)),
        "auto_resume_stop",
        "controller_stop",
        "shutdown",
    ]
