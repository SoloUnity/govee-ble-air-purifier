import importlib
import sys
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.models import PurifierState
from tests.helpers.ha_stubs import (
    CoordinatorEntity as _CoordinatorEntity,
    DeviceInfo as _DeviceInfo,
    install_modules,
)


MODULE_NAME = "custom_components.govee_ble_air_purifier.sensor"


class _SensorDeviceClass:
    PM25 = "pm25"


class _SensorStateClass:
    MEASUREMENT = "measurement"


@dataclass(kw_only=True)
class _SensorEntityDescription:
    key: str
    translation_key: str | None = None
    device_class: str | None = None
    entity_category: str | None = None
    native_unit_of_measurement: str | None = None
    state_class: str | None = None
    suggested_unit_of_measurement: str | None = None
    translation_placeholders: dict[str, str] | None = None


class _EntityCategory:
    DIAGNOSTIC = "diagnostic"


def _install_homeassistant_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    install_modules(
        monkeypatch,
        {
            "homeassistant.components.sensor": {
                "SensorDeviceClass": _SensorDeviceClass,
                "SensorEntity": object,
                "SensorEntityDescription": _SensorEntityDescription,
                "SensorStateClass": _SensorStateClass,
            },
            "homeassistant.config_entries": {"ConfigEntry": object},
            "homeassistant.const": {
                "CONCENTRATION_MICROGRAMS_PER_CUBIC_METER": "µg/m³",
                "PERCENTAGE": "%",
            },
            "homeassistant.core": {"HomeAssistant": object},
            "homeassistant.helpers.device_registry": {"DeviceInfo": _DeviceInfo},
            "homeassistant.helpers.entity": {"EntityCategory": _EntityCategory},
            "homeassistant.helpers.entity_platform": {"AddEntitiesCallback": object},
            "homeassistant.helpers.update_coordinator": {
                "CoordinatorEntity": _CoordinatorEntity
            },
        },
    )


def _import_sensor(monkeypatch: pytest.MonkeyPatch):
    _install_homeassistant_modules(monkeypatch)
    sys.modules.pop(MODULE_NAME, None)
    sys.modules.pop("custom_components.govee_ble_air_purifier.entity", None)
    return importlib.import_module(MODULE_NAME)


@pytest.mark.asyncio
async def test_sensor_platform_surfaces_pm25_and_filter_life(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _import_sensor(monkeypatch)
    coordinator = SimpleNamespace(
        data=PurifierState(is_on=True, pm25=8, filter_life=94, fan_mode="Low"),
        profile=SimpleNamespace(model="H7124"),
    )
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    added_entities = []

    await sensor.async_setup_entry(object(), entry, added_entities.extend)

    assert [entity.entity_description.key for entity in added_entities] == [
        "pm25",
        "filter_life",
    ]
    assert [entity._attr_unique_id for entity in added_entities] == [
        "aabbccddeeff_pm25",
        "aabbccddeeff_filter_life",
    ]
    assert [entity.native_value for entity in added_entities] == [8, 94]


def test_sensor_metadata_matches_cloud_style_entities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _import_sensor(monkeypatch)

    pm25, filter_life = sensor.SENSORS

    assert pm25.translation_key == "pm25"
    assert pm25.device_class == _SensorDeviceClass.PM25
    assert pm25.native_unit_of_measurement == "µg/m³"
    assert pm25.state_class == _SensorStateClass.MEASUREMENT
    assert pm25.entity_category is None

    assert filter_life.translation_key == "filter_life"
    assert filter_life.device_class is None
    assert filter_life.native_unit_of_measurement == "%"
    assert filter_life.state_class == _SensorStateClass.MEASUREMENT
    assert filter_life.entity_category is None


@pytest.mark.asyncio
async def test_pm25_sensor_is_unavailable_with_cached_value_after_update_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _import_sensor(monkeypatch)
    coordinator = SimpleNamespace(
        data=PurifierState(is_on=True, pm25=8, filter_life=94, fan_mode="Low"),
        profile=SimpleNamespace(model="H7124"),
        last_update_success=False,
    )
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    added_entities = []

    await sensor.async_setup_entry(object(), entry, added_entities.extend)
    pm25, filter_life = added_entities

    assert pm25.native_value == 8
    assert pm25.available is False
    assert filter_life.available is False


@pytest.mark.asyncio
async def test_pm25_sensor_is_unavailable_without_cached_value_after_update_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _import_sensor(monkeypatch)
    coordinator = SimpleNamespace(
        data=PurifierState(is_on=True, pm25=None, filter_life=94, fan_mode="Low"),
        profile=SimpleNamespace(model="H7124"),
        last_update_success=False,
    )
    entry = SimpleNamespace(
        unique_id="aabbccddeeff",
        data={"name": "Bedroom Purifier"},
        runtime_data=SimpleNamespace(coordinator=coordinator),
    )
    added_entities = []

    await sensor.async_setup_entry(object(), entry, added_entities.extend)
    pm25, _filter_life = added_entities

    assert pm25.native_value is None
    assert pm25.available is False


def test_sensor_descriptions_include_home_assistant_required_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensor = _import_sensor(monkeypatch)

    for description in sensor.SENSORS:
        assert hasattr(description, "suggested_unit_of_measurement")
        assert hasattr(description, "translation_placeholders")
