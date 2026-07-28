"""Night-light entity for supported Govee BLE air purifiers."""

from __future__ import annotations

from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .entity import GoveeAirPurifierEntity


def _brightness_to_percent(brightness: int) -> int:
    """Convert Home Assistant brightness to the device percentage scale."""

    return max(1, min(100, (brightness * 100 + 127) // 255))


def _percent_to_brightness(percent: int) -> int:
    """Convert a device percentage to Home Assistant brightness."""

    return max(1, min(255, (percent * 255 + 50) // 100))


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up a profile-supported purifier night light."""

    if entry.runtime_data.profile.night_light is None:
        return
    async_add_entities(
        [GoveeNightLight(entry.runtime_data.coordinator, entry)]
    )


class GoveeNightLight(GoveeAirPurifierEntity, LightEntity):
    """Control a purifier's RGB night light."""

    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_translation_key = "night_light"

    def __init__(self, coordinator, entry) -> None:
        """Initialize the night-light entity."""

        super().__init__(coordinator, entry, "night_light")

    @property
    def is_on(self) -> bool | None:
        """Return whether the night light is on."""

        state = self._night_light_state
        return None if state is None else state.is_on

    @property
    def color_mode(self) -> ColorMode | None:
        """Return RGB while the light is known to be on."""

        return ColorMode.RGB if self.is_on is True else None

    @property
    def brightness(self) -> int | None:
        """Return brightness on Home Assistant's 1-255 scale."""

        state = self._night_light_state
        if state is None or state.brightness_percent is None:
            return None
        return _percent_to_brightness(state.brightness_percent)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the queried or command-confirmed RGB color."""

        state = self._night_light_state
        return None if state is None else state.rgb_color

    @property
    def _night_light_state(self):
        """Return the cached night-light state without performing I/O."""

        data = self.coordinator.data
        return None if data is None else data.night_light

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the night light and apply supplied settings."""

        try:
            brightness = kwargs.get(ATTR_BRIGHTNESS)
            if brightness == 0:
                await self.coordinator.async_set_night_light(is_on=False)
                return
            brightness_percent = (
                _brightness_to_percent(brightness)
                if brightness is not None
                else None
            )
            rgb = kwargs.get(ATTR_RGB_COLOR)
            rgb_color = tuple(rgb) if rgb is not None else None
            await self.coordinator.async_set_night_light(
                is_on=True,
                brightness_percent=brightness_percent,
                rgb_color=rgb_color,
            )
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn night light on: {err}") from err

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the night light."""

        try:
            await self.coordinator.async_set_night_light(is_on=False)
        except Exception as err:
            raise HomeAssistantError(f"Failed to turn night light off: {err}") from err
