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

from .auto_resume import (
    ATTR_AUTO_RESUME_CUSTOM_SPEED,
    ATTR_AUTO_RESUME_MODE,
    ATTR_AUTO_RESUME_SUSPENDED,
    AUTO_MODE_CUSTOM,
    AUTO_MODE_HARDWARE,
)
from .entity import GoveeAirPurifierEntity
from .profiles import fan_mode_labels

PRESET_MANUAL = "Manual"
PRESET_AUTO = "Auto"
MANUAL_SPEED_ORDER = ["Sleep", "Low", "Medium", "High", "Turbo"]


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
                entry.runtime_data.auto_resume,
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

    def __init__(self, coordinator, entry, controller, auto_resume) -> None:
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
        self._auto_resume = auto_resume

    async def async_added_to_hass(self) -> None:
        """Subscribe to automatic-mode intent changes."""

        await super().async_added_to_hass()
        self.async_on_remove(
            self._auto_resume.async_add_listener(self._handle_auto_resume_update)
        )

    def _handle_auto_resume_update(self) -> None:
        """Write automatic-mode intent changes to Home Assistant."""

        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, str | bool | int | None]:
        """Persist automatic-mode intent across reloads and restarts."""

        state = self._auto_resume.state
        return {
            ATTR_AUTO_RESUME_MODE: state.mode,
            ATTR_AUTO_RESUME_SUSPENDED: state.suspended,
            ATTR_AUTO_RESUME_CUSTOM_SPEED: self._auto_resume.custom_speed,
        }

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
        if self._controller.active:
            return self._controller.current_speed
        if data is None or data.is_on is False or data.fan_mode not in self._manual_speeds:
            return None
        return ordered_list_item_to_percentage(self._manual_speeds, data.fan_mode)

    @property
    def preset_mode(self) -> str | None:
        """Return Auto or Manual for the fan preset control."""

        data = self.coordinator.data
        if self._auto_resume.state.mode in (AUTO_MODE_HARDWARE, AUTO_MODE_CUSTOM):
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
            await self._auto_resume.async_turn_on()
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier on: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the purifier off."""

        try:
            await self._auto_resume.async_turn_off()
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
            await self._auto_resume.async_set_manual_mode(speed)
        except Exception as err:
            raise HomeAssistantError(f"Failed to set purifier speed: {err}") from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set Auto or Manual preset mode."""

        try:
            if preset_mode == PRESET_AUTO:
                if not self._controller.active:
                    self._current_or_last_manual_speed()
                await self._auto_resume.async_set_hardware_auto()
                return
            if preset_mode == PRESET_MANUAL:
                speed = self._current_or_last_manual_speed()
                if speed is None:
                    raise ValueError("This purifier profile has no manual fan speeds")
                await self._auto_resume.async_set_manual_mode(speed)
                return
            raise ValueError(f"Unsupported preset mode: {preset_mode}")
        except Exception as err:
            raise HomeAssistantError(f"Failed to set purifier preset: {err}") from err

    def _current_or_last_manual_speed(self) -> str | None:
        """Return the current manual speed, previous manual speed, or default."""

        data = self.coordinator.data
        if data is not None and data.fan_mode in self._manual_speeds:
            self._last_manual_speed = data.fan_mode
            return data.fan_mode
        if self._last_manual_speed in self._manual_speeds:
            return self._last_manual_speed
        return self._default_manual_speed
