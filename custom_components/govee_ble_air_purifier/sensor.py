"""Sensor entities for Govee BLE air purifiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONCENTRATION_MICROGRAMS_PER_CUBIC_METER, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import GoveeData
from .entity import GoveeAirPurifierEntity


@dataclass(frozen=True)
class SensorDescription:
    """Description for a purifier sensor."""

    key: str
    translation_key: str
    value_fn: Callable[[GoveeData], int | None]
    device_class: SensorDeviceClass | None = None
    native_unit_of_measurement: str | None = None
    state_class: SensorStateClass | None = None
    entity_category: EntityCategory | None = None


SENSORS = (
    SensorDescription(
        key="pm25",
        translation_key="pm25",
        value_fn=lambda data: data.pm25,
        device_class=SensorDeviceClass.PM25,
        native_unit_of_measurement=CONCENTRATION_MICROGRAMS_PER_CUBIC_METER,
        state_class=SensorStateClass.MEASUREMENT,
    ),
    SensorDescription(
        key="filter_life",
        translation_key="filter_life",
        value_fn=lambda data: data.filter_life,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up purifier sensors."""

    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        [GoveeSensor(coordinator, entry, description) for description in SENSORS]
    )


class GoveeSensor(GoveeAirPurifierEntity, SensorEntity):
    """Purifier sensor entity."""

    def __init__(self, coordinator, entry, description: SensorDescription) -> None:
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description
        self._attr_translation_key = description.translation_key
        self._attr_device_class = description.device_class
        self._attr_native_unit_of_measurement = description.native_unit_of_measurement
        self._attr_state_class = description.state_class
        self._attr_entity_category = description.entity_category

    @property
    def native_value(self) -> int | None:
        """Return sensor value."""

        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
