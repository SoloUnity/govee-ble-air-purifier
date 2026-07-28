"""Data coordinator for Govee BLE air purifiers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import timedelta
import logging
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_POLLING_INTERVAL_SECONDS
from .models import NightLightState, PurifierState
from .profiles import H7124_PROFILE, ModelProfile

POLLING_INTERVAL = timedelta(seconds=DEFAULT_POLLING_INTERVAL_SECONDS)
LOGGER = logging.getLogger(__name__)


def _merge_night_light_state(
    current: NightLightState | None, update: NightLightState | None
) -> NightLightState | None:
    """Merge known night-light fields without erasing command-confirmed state."""

    if update is None:
        return current
    current = current or NightLightState()
    return NightLightState(
        is_on=update.is_on if update.is_on is not None else current.is_on,
        brightness_percent=(
            update.brightness_percent
            if update.brightness_percent is not None
            else current.brightness_percent
        ),
        rgb_color=(
            update.rgb_color if update.rgb_color is not None else current.rgb_color
        ),
    )

@dataclass
class GoveeRuntimeData:
    """Runtime objects attached to a Home Assistant config entry."""

    coordinator: "GoveeCoordinator"
    profile: ModelProfile
    controller: Any
    auto_resume: Any


class GoveeCoordinator(DataUpdateCoordinator):
    """Coordinate BLE polling and command-side refreshes."""

    def __init__(
        self,
        hass: Any,
        client: Any,
        *,
        profile: ModelProfile = H7124_PROFILE,
        polling_interval: timedelta = POLLING_INTERVAL,
    ) -> None:
        self._hass = hass
        self.client = client
        self.profile = profile
        self.polling_interval = polling_interval
        self.data: PurifierState | None = None
        self.last_poll_success = False
        self.last_pm25_update_success = False
        self.pm25_sample_revision = 0
        self.poll_revision = 0
        self._last_fan_mode: str | None = None
        self._state_lock = asyncio.Lock()
        self._background_refresh_task: asyncio.Task[Any] | None = None
        super().__init__(
            hass,
            LOGGER,
            name="Govee BLE Air Purifier",
            update_interval=polling_interval,
        )

    def _publish_data(self, data: PurifierState) -> None:
        """Publish coordinator data to subscribed entities immediately."""

        self.async_set_updated_data(data)

    def _schedule_background_refresh(self) -> None:
        """Refresh later without blocking command UI updates."""

        self._cancel_background_refresh()

        async def refresh_later() -> None:
            try:
                await asyncio.sleep(1)
                await self.async_request_refresh()
            finally:
                if self._background_refresh_task is task:
                    self._background_refresh_task = None

        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            task = self._hass.async_create_task(refresh_later())
        else:
            task = asyncio.create_task(refresh_later())
        self._background_refresh_task = task

    def _cancel_background_refresh(self) -> asyncio.Task[Any] | None:
        """Cancel a scheduled refresh so commands can use BLE first."""

        task = self._background_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._background_refresh_task = None
        return task

    async def async_shutdown(self) -> None:
        """Cancel refreshes, stop polling, and close the BLE client."""

        try:
            task = self._cancel_background_refresh()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await super().async_shutdown()
        finally:
            await self.client.async_close()

    async def _async_update_data(self) -> PurifierState:
        """Fetch current state from the BLE client."""

        async with self._state_lock:
            try:
                client_data = await self.client.async_get_state()
            except Exception as err:  # pragma: no cover - depends on HA runtime
                self.last_poll_success = False
                self.last_pm25_update_success = False
                raise UpdateFailed(str(err)) from err
            self.last_poll_success = True
            self.poll_revision += 1
            self.last_pm25_update_success = client_data.pm25 is not None
            if self.last_pm25_update_success:
                self.pm25_sample_revision += 1
            current = self.data or PurifierState()
            data = PurifierState(
                is_on=client_data.is_on,
                pm25=(
                    client_data.pm25 if client_data.pm25 is not None else current.pm25
                ),
                filter_life=client_data.filter_life,
                fan_mode=self._last_fan_mode or client_data.fan_mode,
                night_light=(
                    _merge_night_light_state(
                        current.night_light, client_data.night_light
                    )
                    if self.profile.night_light is not None
                    else None
                ),
            )
            self.data = data
            return data

    async def async_request_refresh(self) -> None:
        """Request a coordinator refresh."""

        await super().async_request_refresh()

    async def async_set_power(self, is_on: bool) -> None:
        """Set power and refresh shared state."""

        self._cancel_background_refresh()
        async with self._state_lock:
            result = await self.client.async_set_power(is_on)
            confirmed_is_on = is_on if result is None else result
            if not confirmed_is_on:
                self._last_fan_mode = None
            current = self.data or PurifierState()
            self._publish_data(
                replace(
                    current,
                    is_on=confirmed_is_on,
                    fan_mode=current.fan_mode if confirmed_is_on else None,
                )
            )
        self._schedule_background_refresh()

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set fan mode, powering on first if needed."""

        if mode not in self.profile.fan_mode_commands:
            raise ValueError(f"Unsupported fan mode: {mode}")
        self._cancel_background_refresh()
        if self.data is None:
            await self.async_request_refresh()
        async with self._state_lock:
            if self.data is not None and self.data.is_on is False:
                if hasattr(self.client, "async_set_power_and_fan_mode"):
                    result = await self.client.async_set_power_and_fan_mode(mode)
                    confirmed_is_on = (
                        result.is_on
                        if isinstance(result, PurifierState)
                        and result.is_on is not None
                        else True
                    )
                    confirmed_mode = (
                        result.fan_mode
                        if isinstance(result, PurifierState)
                        and result.fan_mode is not None
                        else mode
                    )
                else:
                    power_result = await self.client.async_set_power(True)
                    mode_result = await self.client.async_set_fan_mode(mode)
                    confirmed_is_on = True if power_result is None else power_result
                    confirmed_mode = mode if mode_result is None else mode_result
            else:
                mode_result = await self.client.async_set_fan_mode(mode)
                confirmed_is_on = True if self.data is None else self.data.is_on
                confirmed_mode = mode if mode_result is None else mode_result
            self._last_fan_mode = confirmed_mode
            current = self.data or PurifierState()
            self._publish_data(
                replace(
                    current,
                    is_on=confirmed_is_on,
                    fan_mode=confirmed_mode,
                )
            )
        self._schedule_background_refresh()

    async def async_set_night_light(
        self,
        *,
        is_on: bool,
        brightness_percent: int | None = None,
        rgb_color: tuple[int, int, int] | None = None,
    ) -> None:
        """Set night-light power and optional settings in confirmed order."""

        if self.profile.night_light is None:
            raise ValueError("This purifier profile has no night-light capability")
        if not is_on and (brightness_percent is not None or rgb_color is not None):
            raise ValueError("Night-light settings cannot accompany power off")

        self._cancel_background_refresh()
        if self.data is None:
            await self.async_request_refresh()

        confirmed_any = False
        try:
            async with self._state_lock:
                current = self.data or PurifierState(
                    night_light=NightLightState()
                )
                night_light = current.night_light or NightLightState()
                has_settings = brightness_percent is not None or rgb_color is not None
                should_set_power = not is_on or not has_settings or night_light.is_on is not True

                if should_set_power:
                    update = await self.client.async_set_night_light_power(is_on)
                    night_light = _merge_night_light_state(night_light, update)
                    current = replace(current, night_light=night_light)
                    self._publish_data(current)
                    confirmed_any = True

                if is_on and brightness_percent is not None:
                    update = await self.client.async_set_night_light_brightness(
                        brightness_percent
                    )
                    night_light = _merge_night_light_state(night_light, update)
                    current = replace(current, night_light=night_light)
                    self._publish_data(current)
                    confirmed_any = True

                if is_on and rgb_color is not None:
                    update = await self.client.async_set_night_light_rgb(rgb_color)
                    night_light = _merge_night_light_state(night_light, update)
                    current = replace(current, night_light=night_light)
                    self._publish_data(current)
                    confirmed_any = True
        finally:
            if confirmed_any:
                self._schedule_background_refresh()
