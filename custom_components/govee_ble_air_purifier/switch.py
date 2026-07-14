"""Switch entity for integration-managed Custom Auto control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GoveeAirPurifierEntity

PRESET_AUTO = "Auto"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Custom Auto switch entity."""

    async_add_entities(
        [
            GoveeCustomAutoSwitch(
                entry.runtime_data.coordinator,
                entry,
                entry.runtime_data.controller,
            )
        ]
    )


class GoveeCustomAutoSwitch(GoveeAirPurifierEntity, SwitchEntity):
    """Control whether Home Assistant Custom Auto owns fan speed."""

    _attr_translation_key = "custom_auto"

    def __init__(self, coordinator, entry, controller) -> None:
        """Initialize the Custom Auto switch."""

        super().__init__(coordinator, entry, "custom_auto")
        self._controller = controller

    async def async_added_to_hass(self) -> None:
        """Subscribe to controller state changes."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.async_add_listener(self._handle_controller_update)
        )

    @property
    def is_on(self) -> bool:
        """Return whether Custom Auto currently owns fan speed."""

        return self._controller.active

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate Custom Auto, powering on the purifier if needed."""

        try:
            if not self._controller.active:
                await self._controller.async_activate()
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable Custom Auto: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hand control from Custom Auto to the purifier's hardware Auto mode."""

        if not self._controller.active:
            return
        try:
            await self._controller.async_deactivate()
            await self.coordinator.async_set_fan_mode(PRESET_AUTO)
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable Custom Auto: {err}") from err

    def _handle_controller_update(self) -> None:
        """Publish controller ownership changes to Home Assistant."""

        self.async_write_ha_state()
