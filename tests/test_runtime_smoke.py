"""Smoke tests that run only with a real Home Assistant installation."""

from importlib import import_module
from importlib.metadata import version
from types import SimpleNamespace

import pytest


INTEGRATION_PACKAGE = "custom_components.govee_ble_air_purifier"
RUNTIME_MODULES = (
    "client",
    "config_flow",
    "controller",
    "coordinator",
    "diagnostics",
    "entity",
    "fan",
    "select",
    "sensor",
    "switch",
)


def test_runtime_modules_import_with_real_home_assistant() -> None:
    """Import all HA-facing modules without the lightweight suite's stubs."""
    homeassistant = import_module("homeassistant")

    assert version("homeassistant") == "2026.7.2"
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


@pytest.mark.asyncio
async def test_platform_setup_uses_real_home_assistant_entities() -> None:
    """Construct each configured platform's entities through its setup hook."""
    from homeassistant.components.fan import FanEntity
    from homeassistant.components.sensor import SensorEntity
    from homeassistant.components.switch import SwitchEntity

    from custom_components.govee_ble_air_purifier.coordinator import GoveeData
    from custom_components.govee_ble_air_purifier.fan import async_setup_entry as setup_fan
    from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE
    from custom_components.govee_ble_air_purifier.sensor import (
        async_setup_entry as setup_sensor,
    )
    from custom_components.govee_ble_air_purifier.switch import (
        async_setup_entry as setup_switch,
    )

    coordinator = SimpleNamespace(
        async_add_listener=lambda listener: lambda: None,
        data=GoveeData(is_on=True, pm25=7, filter_life=95, fan_mode="Low"),
        last_update_success=True,
        profile=H7124_PROFILE,
    )
    controller = SimpleNamespace(
        active=False,
        async_add_listener=lambda listener: lambda: None,
    )
    entry = SimpleNamespace(
        data={"name": "Runtime smoke purifier"},
        runtime_data=SimpleNamespace(
            controller=controller,
            coordinator=coordinator,
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
