"""Select entities for Govee BLE air purifiers."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GoveeAirPurifierEntity
from .profiles import fan_mode_labels


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up fan mode selector."""

    async_add_entities([GoveeFanModeSelect(entry.runtime_data.coordinator, entry)])


class GoveeFanModeSelect(GoveeAirPurifierEntity, SelectEntity):
    """Fan speed/mode selector without an Off option."""

    _attr_translation_key = "fan_mode"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, "fan_mode")
        self._attr_options = fan_mode_labels(coordinator.profile)

    @property
    def current_option(self) -> str | None:
        """Return last commanded fan mode."""

        return None if self.coordinator.data is None else self.coordinator.data.fan_mode

    async def async_select_option(self, option: str) -> None:
        """Select fan speed/mode."""

        try:
            await self.coordinator.async_set_fan_mode(option)
        except Exception as err:
            raise HomeAssistantError(f"Failed to set fan mode: {err}") from err
