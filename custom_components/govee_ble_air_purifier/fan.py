"""Fan entity for Govee BLE air purifiers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util.percentage import (
    ordered_list_item_to_percentage,
    percentage_to_ordered_list_item,
)

from .entity import GoveeAirPurifierEntity
from .profiles import fan_mode_labels

PRESET_MANUAL = "Manual"
# Fan modes the firmware treats as top-level work modes (they emit `ee 05`
# mode pushes and are not part of the gear-speed ladder). These map to the
# segmented preset picker in the Home Assistant UI, mirroring the Govee
# cloud capability model used by upstream integrations.
MODE_PRESET_ORDER = ("Auto", "Sleep", "Turbo")
# Manual gear speeds the firmware exposes via `3a 05 01 <n>` frames. These
# render as the stepped percentage control (buttons when count <= 3).
GEAR_SPEED_ORDER = ("Low", "Medium", "High")


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up air purifier fan entity."""

    async_add_entities([GoveeAirPurifierFan(entry.runtime_data.coordinator, entry)])


class GoveeAirPurifierFan(GoveeAirPurifierEntity, FanEntity):
    """Cloud-style fan entity for the purifier."""

    _attr_supported_features = (
        FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
        | FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
    )

    def __init__(self, coordinator, entry) -> None:
        """Initialize the fan entity."""

        super().__init__(coordinator, entry, "fan")
        self._attr_name = None
        profile_modes = fan_mode_labels(coordinator.profile)

        ordered_gear_speeds = [
            mode for mode in GEAR_SPEED_ORDER if mode in profile_modes
        ]
        extra_gear_speeds = [
            mode
            for mode in profile_modes
            if mode not in ordered_gear_speeds and mode not in MODE_PRESET_ORDER
        ]
        self._manual_speeds = ordered_gear_speeds + extra_gear_speeds
        self._attr_speed_count = len(self._manual_speeds)

        ordered_mode_presets = [
            mode for mode in MODE_PRESET_ORDER if mode in profile_modes
        ]
        extra_mode_presets = [
            mode
            for mode in profile_modes
            if mode not in self._manual_speeds and mode not in ordered_mode_presets
        ]
        self._mode_presets = ordered_mode_presets + extra_mode_presets
        self._attr_preset_modes = [PRESET_MANUAL] + self._mode_presets
        self._last_manual_speed = self._default_manual_speed

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
        """Return current manual gear speed as a Home Assistant percentage."""

        data = self.coordinator.data
        if data is None or data.is_on is False or data.fan_mode not in self._manual_speeds:
            return None
        return ordered_list_item_to_percentage(self._manual_speeds, data.fan_mode)

    @property
    def preset_mode(self) -> str | None:
        """Return the active preset mode (Manual or a named mode preset)."""

        data = self.coordinator.data
        if data is None:
            return None
        if data.fan_mode in self._mode_presets:
            return data.fan_mode
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
            await self.coordinator.async_set_power(False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn purifier off: {err}") from err

    async def async_set_percentage(self, percentage: int) -> None:
        """Set manual purifier gear speed from a Home Assistant percentage."""

        try:
            if percentage == 0:
                await self.async_turn_off()
                return
            if not self._manual_speeds:
                raise ValueError("This purifier profile has no manual fan speeds")
            speed = percentage_to_ordered_list_item(self._manual_speeds, percentage)
            self._last_manual_speed = speed
            await self.coordinator.async_set_fan_mode(speed)
        except Exception as err:
            raise HomeAssistantError(f"Failed to set purifier speed: {err}") from err

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set Auto, Sleep, Turbo, or Manual preset mode."""

        try:
            if preset_mode in self._mode_presets:
                await self.coordinator.async_set_fan_mode(preset_mode)
                return
            if preset_mode == PRESET_MANUAL:
                speed = self._current_or_last_manual_speed()
                if speed is None:
                    raise ValueError("This purifier profile has no manual fan speeds")
                await self.coordinator.async_set_fan_mode(speed)
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