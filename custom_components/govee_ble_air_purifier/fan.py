"""Fan entity for Govee BLE air purifiers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .entity import GoveeAirPurifierEntity
from .profiles import fan_mode_labels

PRESET_MANUAL = "Manual"
PRESET_AUTO = "Auto"
MANUAL_SPEED_ORDER = ["Sleep", "Low", "Medium", "High", "Turbo"]
ATTR_CUSTOM_AUTO_ACTIVE = "custom_auto_active"
ATTR_CUSTOM_AUTO_SPEED = "custom_auto_speed"


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up air purifier fan entity."""

    async_add_entities(
        [
            GoveeAirPurifierFan(
                entry.runtime_data.coordinator,
                entry,
                entry.runtime_data.controller,
            )
        ]
    )


class GoveeAirPurifierFan(GoveeAirPurifierEntity, FanEntity, RestoreEntity):
    """Cloud-style fan entity for the purifier."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.PRESET_MODE
    )
    if hasattr(FanEntityFeature, "TURN_ON"):
        _attr_supported_features |= FanEntityFeature.TURN_ON
    if hasattr(FanEntityFeature, "TURN_OFF"):
        _attr_supported_features |= FanEntityFeature.TURN_OFF

    def __init__(self, coordinator, entry, controller=None) -> None:
        """Initialize the fan entity."""

        super().__init__(coordinator, entry, "fan")
        self._attr_name = None
        profile_modes = fan_mode_labels(coordinator.profile)
        ordered_manual_speeds = [
            mode for mode in MANUAL_SPEED_ORDER if mode in profile_modes
        ]
        extra_manual_speeds = [
            mode
            for mode in profile_modes
            if mode not in ordered_manual_speeds and mode != PRESET_AUTO
        ]
        self._manual_speeds = ordered_manual_speeds + extra_manual_speeds
        self._attr_speed_count = len(self._manual_speeds)
        self._attr_preset_modes = [PRESET_MANUAL]
        if PRESET_AUTO in profile_modes:
            self._attr_preset_modes.append(PRESET_AUTO)
        self._last_manual_speed = self._default_manual_speed
        self._controller = controller

    async def async_added_to_hass(self) -> None:
        """Restore custom-auto ownership and its underlying manual speed."""

        await super().async_added_to_hass()
        if self._controller is None:
            return
        self.async_on_remove(
            self._controller.async_add_listener(self._handle_controller_update)
        )
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        attributes = last_state.attributes
        if attributes.get(ATTR_CUSTOM_AUTO_ACTIVE) is not True:
            return
        if not self._controller.enabled:
            await self.coordinator.async_set_fan_mode(PRESET_AUTO)
            return
        restored_speed = attributes.get(ATTR_CUSTOM_AUTO_SPEED)
        if restored_speed not in (20, 40, 60, 80, 100):
            restored_speed = None
        await self._controller.async_activate(
            restored_speed=restored_speed, restoring=True
        )

    def _handle_controller_update(self) -> None:
        """Write logical custom-auto ownership changes to Home Assistant."""

        self.async_write_ha_state()

    @property
    def _default_manual_speed(self) -> str | None:
        """Return the default manual speed for Manual preset selection."""

        if "Medium" in self._manual_speeds:
            return "Medium"
        return self._manual_speeds[0] if self._manual_speeds else None

    @property
    def is_on(self) -> bool | None:
        """Return true when purifier power is on."""

        return None if self.coordinator.data is None else self.coordinator.data.is_on

    @property
    def percentage(self) -> int | None:
        """Return current manual speed as a Home Assistant percentage."""

        data = self.coordinator.data
        if self._controller is not None and self._controller.active:
            return self._controller.current_speed
        if data is None or data.is_on is False or data.fan_mode not in self._manual_speeds:
            return None
        return ordered_list_item_to_percentage(self._manual_speeds, data.fan_mode)

    @property
    def preset_mode(self) -> str | None:
        """Return Auto or Manual for the fan preset control."""

        data = self.coordinator.data
        if self._controller is not None and self._controller.active:
            return PRESET_AUTO
        if data is None:
            return None
        if data.fan_mode == PRESET_AUTO:
            return PRESET_AUTO
        if data.fan_mode in self._manual_speeds or data.is_on:
            return PRESET_MANUAL
        return None

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        """Turn the purifier on."""

        try:
            if percentage is not None:
                await self.async_set_percentage(percentage)
                return
            if preset_mode is not None:
                await self.async_set_preset_mode(preset_mode)
                return
            await self.coordinator.async_set_power(True)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier on: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the purifier off."""

        try:
            await self._async_disable_custom_auto()
            await self.coordinator.async_set_power(False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier off: {err}") from err

    async def async_set_percentage(self, percentage: int) -> None:
        """Set manual purifier speed from a Home Assistant percentage."""

        try:
            if percentage == 0:
                await self.async_turn_off()
                return
            if not self._manual_speeds:
                raise ValueError("This purifier profile has no manual fan speeds")
            speed = percentage_to_ordered_list_item(self._manual_speeds, percentage)
            self._last_manual_speed = speed
            await self._async_disable_custom_auto()
            await self.coordinator.async_set_fan_mode(speed)
        except Exception as err:
            raise HomeAssistantError(f"Failed to set purifier speed: {err}") from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set Auto or Manual preset mode."""

        try:
            if preset_mode == PRESET_AUTO:
                if self._controller is not None and self._controller.enabled:
                    await self._controller.async_activate()
                    return
                await self._async_disable_custom_auto()
                await self.coordinator.async_set_fan_mode(PRESET_AUTO)
                return
            if preset_mode == PRESET_MANUAL:
                await self._async_disable_custom_auto()
                speed = self._current_or_last_manual_speed()
                if speed is None:
                    raise ValueError("This purifier profile has no manual fan speeds")
                await self.coordinator.async_set_fan_mode(speed)
                return
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        except Exception as err:
            raise HomeAssistantError(f"Failed to set purifier preset: {err}") from err

    @property
    def extra_state_attributes(self) -> dict[str, bool | int | None]:
        """Persist logical custom-auto state across reloads and restarts."""

        return {
            ATTR_CUSTOM_AUTO_ACTIVE: bool(
                self._controller is not None and self._controller.active
            ),
            ATTR_CUSTOM_AUTO_SPEED: (
                self._controller.current_speed
                if self._controller is not None and self._controller.active
                else None
            ),
        }

    async def _async_disable_custom_auto(self) -> None:
        """Release custom-auto control before an explicit user command."""

        if self._controller is not None and self._controller.active:
            await self._controller.async_deactivate()

    def _current_or_last_manual_speed(self) -> str | None:
        """Return the current manual speed, previous manual speed, or default."""

        data = self.coordinator.data
        if data is not None and data.fan_mode in self._manual_speeds:
            self._last_manual_speed = data.fan_mode
            return data.fan_mode
        if self._last_manual_speed in self._manual_speeds:
            return self._last_manual_speed
        return self._default_manual_speed
