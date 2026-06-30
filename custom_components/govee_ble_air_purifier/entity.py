"""Base entity helpers."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER


class GoveeAirPurifierEntity(CoordinatorEntity):
    """Base entity for the purifier."""

    _attr_has_entity_name = True


    def __init__(self, coordinator, entry, key: str) -> None:
        """Initialize the entity."""

        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer=MANUFACTURER,
            model=coordinator.profile.model,
            name=entry.data.get("name", "Govee H7124 Air Purifier"),
        )
