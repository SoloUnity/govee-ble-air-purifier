"""Switch entity for integration-managed Custom Auto control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .auto_resume import (
    ATTR_AUTO_RESUME_CUSTOM_SPEED,
    ATTR_AUTO_RESUME_MODE,
    ATTR_AUTO_RESUME_SUSPENDED,
    AUTO_MODE_CUSTOM,
)
from .entity import GoveeAirPurifierEntity

ATTR_CUSTOM_AUTO_ACTIVE = "custom_auto_active"
ATTR_CUSTOM_AUTO_SPEED = "custom_auto_speed"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the Custom Auto switch entity."""

    if not entry.runtime_data.coordinator.profile.supports_custom_auto:
        return
    async_add_entities(
        [
            GoveeCustomAutoSwitch(
                entry.runtime_data.coordinator,
                entry,
                entry.runtime_data.controller,
                entry.runtime_data.auto_resume,
            )
        ]
    )


class GoveeCustomAutoSwitch(GoveeAirPurifierEntity, SwitchEntity, RestoreEntity):
    """Control whether Home Assistant Custom Auto owns fan speed."""

    _attr_translation_key = "custom_auto"

    def __init__(self, coordinator, entry, controller, auto_resume) -> None:
        """Initialize the Custom Auto switch."""

        super().__init__(coordinator, entry, "custom_auto")
        self._controller = controller
        self._auto_resume = auto_resume

    async def async_added_to_hass(self) -> None:
        """Subscribe to logical Custom Auto selection and activity."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.async_add_listener(self._handle_controller_update)
        )
        self.async_on_remove(
            self._auto_resume.async_add_listener(self._handle_controller_update)
        )

    @property
    def is_on(self) -> bool:
        """Return whether Custom Auto currently owns fan speed."""

        return self._auto_resume.state.mode == AUTO_MODE_CUSTOM

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Activate Custom Auto, powering on the purifier if needed."""

        try:
            await self._auto_resume.async_enable_custom_auto()
        except Exception as err:
            raise HomeAssistantError(f"Failed to enable Custom Auto: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Hand control from Custom Auto to the purifier's hardware Auto mode."""

        try:
            await self._auto_resume.async_disable_custom_auto()
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable Custom Auto: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | int | None]:
        """Persist automatic-mode intent and expose Custom Auto activity."""

        state = self._auto_resume.state
        return {
            ATTR_CUSTOM_AUTO_ACTIVE: self._controller.active,
            ATTR_CUSTOM_AUTO_SPEED: self._auto_resume.custom_speed,
            ATTR_AUTO_RESUME_MODE: state.mode,
            ATTR_AUTO_RESUME_SUSPENDED: state.suspended,
            ATTR_AUTO_RESUME_CUSTOM_SPEED: self._auto_resume.custom_speed,
        }

    def _handle_controller_update(self) -> None:
        """Publish controller ownership changes to Home Assistant."""

        self.async_write_ha_state()
