"""Base entity helpers."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_ADDRESS, DOMAIN, MANUFACTURER


class GoveeAirPurifierEntity(CoordinatorEntity):
    """Base entity for the purifier."""

    _attr_has_entity_name = True


    def __init__(self, coordinator, entry, key: str) -> None:
        """Initialize the entity."""

        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        name = entry.data.get("name") or coordinator.profile.display_name
        address = entry.data.get(CONF_ADDRESS)
        self._attr_device_info = DeviceInfo(
            **(
                {"connections": {(CONNECTION_BLUETOOTH, address)}}
                if isinstance(address, str)
                else {}
            ),
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer=MANUFACTURER,
            model=coordinator.profile.model,
            name=name,
        )
