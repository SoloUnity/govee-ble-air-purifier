"""Switch entities for Govee BLE air purifiers."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GoveeAirPurifierEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up power switch."""

    async_add_entities([GoveePowerSwitch(entry.runtime_data.coordinator, entry)])


class GoveePowerSwitch(GoveeAirPurifierEntity, SwitchEntity):
    """Power switch for the purifier."""

    _attr_translation_key = "power"


    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "power")

    @property
    def is_on(self) -> bool | None:
        """Return true when purifier power is on."""

        return None if self.coordinator.data is None else self.coordinator.data.is_on

    async def async_turn_on(self, **kwargs) -> None:
        """Turn the purifier on."""

        try:
            await self.coordinator.async_set_power(True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier on: {err}") from err

    async def async_turn_off(self, **kwargs) -> None:
        """Turn the purifier off."""

        try:
            await self.coordinator.async_set_power(False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier off: {err}") from err
