"""Switch entity for integration-managed Custom Auto control."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import restore_state
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import GoveeAirPurifierEntity

PRESET_AUTO = "Auto"
ATTR_CUSTOM_AUTO_ACTIVE = "custom_auto_active"
ATTR_CUSTOM_AUTO_SPEED = "custom_auto_speed"
CUSTOM_AUTO_SPEEDS = (20, 40, 60, 80, 100)


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


class GoveeCustomAutoSwitch(GoveeAirPurifierEntity, SwitchEntity, RestoreEntity):
    """Control whether Home Assistant Custom Auto owns fan speed."""

    _attr_translation_key = "custom_auto"

    def __init__(self, coordinator, entry, controller) -> None:
        """Initialize the Custom Auto switch."""

        super().__init__(coordinator, entry, "custom_auto")
        self._controller = controller

    async def async_added_to_hass(self) -> None:
        """Subscribe to and restore logical custom-auto ownership."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._controller.async_add_listener(self._handle_controller_update)
        )
        restored = await self._async_get_last_custom_auto_state()
        if restored is None:
            return
        last_state, legacy_fan_state = restored
        attributes = last_state.attributes
        if (
            attributes.get(ATTR_CUSTOM_AUTO_ACTIVE) is not True
            and (legacy_fan_state or last_state.state != STATE_ON)
        ):
            return
        restored_speed = attributes.get(ATTR_CUSTOM_AUTO_SPEED)
        if restored_speed not in CUSTOM_AUTO_SPEEDS:
            restored_speed = None
        await self._controller.async_activate(
            restored_speed=restored_speed, restoring=True
        )

    async def _async_get_last_custom_auto_state(self) -> tuple[Any, bool] | None:
        """Return switch state or migrate the legacy fan restore record."""

        if (last_state := await self.async_get_last_state()) is not None:
            return last_state, False
        if self.hass is None:
            return None
        fan_entity_id = er.async_get(self.hass).async_get_entity_id(
            "fan", DOMAIN, f"{self._entry.unique_id}_fan"
        )
        if fan_entity_id is None:
            return None
        stored_state = restore_state.async_get(self.hass).last_states.get(fan_entity_id)
        return (stored_state.state, True) if stored_state is not None else None

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
            await self._controller.async_handoff(
                lambda: self.coordinator.async_set_fan_mode(PRESET_AUTO)
            )
        except Exception as err:
            raise HomeAssistantError(f"Failed to disable Custom Auto: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, bool | int | None]:
        """Persist logical custom-auto ownership across reloads and restarts."""

        return {
            ATTR_CUSTOM_AUTO_ACTIVE: self._controller.active,
            ATTR_CUSTOM_AUTO_SPEED: (
                self._controller.current_speed if self._controller.active else None
            ),
        }

    def _handle_controller_update(self) -> None:
        """Publish controller ownership changes to Home Assistant."""

        self.async_write_ha_state()
