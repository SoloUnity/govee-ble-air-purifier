"""Data coordinator for Govee BLE air purifiers."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import timedelta
import logging
from typing import Any

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .govee_ble_air_purifier_protocol import (
    H7124_PROFILE,
    ModelProfile,
    NightLightState,
    PurifierPushUpdate,
    PurifierState,
)

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
        polling_interval: timedelta | None = None,
    ) -> None:
        if polling_interval is None:
            polling_interval = timedelta(seconds=profile.polling_interval_seconds)
        self._hass = hass
        self.client = client
        self.profile = profile
        self.polling_interval = polling_interval
        self.data: PurifierState | None = None
        self.last_poll_success = False
        self.last_pm25_update_success = False
        self.pm25_sample_revision = 0
        self.poll_revision = 0
        self.device_observation_revision = 0
        self._last_fan_mode: str | None = None
        self._command_lock = asyncio.Lock()
        self._state_lock = asyncio.Lock()
        self._command_revision = 0
        self._last_published_command_revision = 0
        self._power_push_revision = 0
        self._fan_mode_push_revision = 0
        self._night_light_push_revision = 0
        self._push_revision = 0
        self._push_updates_enabled = False
        self._push_tasks: set[asyncio.Task[Any]] = set()
        self._physical_mode_handler: Callable[[str], Awaitable[None]] | None = None
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
            await self.async_disable_push_updates()
            task = self._cancel_background_refresh()
            if task is not None:
                await asyncio.gather(task, return_exceptions=True)
            await super().async_shutdown()
        finally:
            await self.client.async_close()

    async def _async_update_data(self) -> PurifierState:
        """Fetch current state from the BLE client."""

        command_revision = self._command_revision
        power_push_revision = self._power_push_revision
        night_light_push_revision = self._night_light_push_revision
        try:
            client_data = await self.client.async_get_state()
        except Exception as err:  # pragma: no cover - depends on HA runtime
            self.last_poll_success = False
            self.last_pm25_update_success = False
            LOGGER.warning("Govee BLE Air Purifier update failed: %s", err)
            raise UpdateFailed(str(err)) from err
        async with self._state_lock:
            self.last_poll_success = True
            self.poll_revision += 1
            self.device_observation_revision += 1
            self.last_pm25_update_success = client_data.pm25 is not None
            if self.last_pm25_update_success:
                self.pm25_sample_revision += 1
            current = self.data or PurifierState()
            data = PurifierState(
                is_on=(
                    current.is_on
                    if self._power_push_revision > power_push_revision
                    else client_data.is_on
                ),
                pm25=(
                    client_data.pm25 if client_data.pm25 is not None else current.pm25
                ),
                filter_life=client_data.filter_life,
                fan_mode=self._last_fan_mode or client_data.fan_mode,
                night_light=(
                    (
                        current.night_light
                        if (
                            self._last_published_command_revision > command_revision
                            or self._night_light_push_revision
                            > night_light_push_revision
                        )
                        else _merge_night_light_state(
                            current.night_light, client_data.night_light
                        )
                    )
                    if self.profile.night_light is not None
                    else None
                ),
            )
            if self._last_published_command_revision > command_revision:
                data = replace(data, is_on=current.is_on)
            self.data = data
            return data

    def async_enable_push_updates(
        self, physical_mode_handler: Callable[[str], Awaitable[None]]
    ) -> None:
        """Enable client push publication after runtime ownership is restored."""

        self._physical_mode_handler = physical_mode_handler
        self._push_updates_enabled = True
        self.client.set_push_callback(self._handle_client_push)

    async def async_disable_push_updates(self) -> None:
        """Detach the client callback and cancel pending push publications."""

        was_enabled = self._push_updates_enabled
        self._push_updates_enabled = False
        self._physical_mode_handler = None
        if was_enabled:
            self.client.set_push_callback(None)
        tasks = tuple(self._push_tasks)
        current_task = asyncio.current_task()
        wait_tasks = tuple(task for task in tasks if task is not current_task)
        for task in wait_tasks:
            if not task.done():
                task.cancel()
        if wait_tasks:
            await asyncio.gather(*wait_tasks, return_exceptions=True)
        self._push_tasks.clear()

    def _handle_client_push(self, update: PurifierPushUpdate) -> None:
        """Schedule one non-blocking purifier push on Home Assistant's loop."""

        if not self._push_updates_enabled:
            return
        coroutine = self._async_apply_push_update(update)
        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            task = self._hass.async_create_task(coroutine)
        else:
            task = asyncio.create_task(coroutine)
        self._push_tasks.add(task)
        task.add_done_callback(self._handle_push_task_done)

    def _handle_push_task_done(self, task: asyncio.Task[Any]) -> None:
        """Observe one completed push task and remove it from lifecycle tracking."""

        self._push_tasks.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            LOGGER.error(
                "Failed to apply purifier push update",
                exc_info=(type(error), error, error.__traceback__),
            )

    async def _async_apply_push_update(self, update: PurifierPushUpdate) -> None:
        """Merge one physical-device observation without treating it as a poll."""

        if not self._push_updates_enabled:
            return
        if update.fan_mode is not None and self._physical_mode_handler is not None:
            try:
                await self._physical_mode_handler(update.fan_mode)
            except Exception:
                LOGGER.exception(
                    "Failed to update automatic-mode ownership from physical push"
                )
        async with self._state_lock:
            if not self._push_updates_enabled:
                return
            current = self.data or PurifierState()
            data = current
            self._push_revision += 1
            revision = self._push_revision
            if update.is_on is not None:
                self._power_push_revision = revision
                self.device_observation_revision += 1
                if not update.is_on:
                    self._last_fan_mode = None
                data = replace(
                    data,
                    is_on=update.is_on,
                    fan_mode=data.fan_mode if update.is_on else None,
                )
            if update.fan_mode is not None:
                self._fan_mode_push_revision = revision
                self._last_fan_mode = update.fan_mode
                data = replace(data, fan_mode=update.fan_mode)
            if update.night_light is not None and self.profile.night_light is not None:
                self._night_light_push_revision = revision
                data = replace(
                    data,
                    night_light=_merge_night_light_state(
                        data.night_light, update.night_light
                    ),
                )
            self.data = data
            self._publish_data(data)

    async def async_request_refresh(self) -> None:
        """Request a coordinator refresh."""

        await super().async_request_refresh()

    async def async_set_power(self, is_on: bool) -> None:
        """Set power and refresh shared state."""

        self._cancel_background_refresh()
        self._command_revision += 1
        command_revision = self._command_revision
        power_push_revision = self._power_push_revision
        async with self._command_lock:
            result = await self.client.async_set_power(is_on)
            confirmed_is_on = is_on if result is None else result
            async with self._state_lock:
                current = self.data or PurifierState()
                published_is_on = (
                    current.is_on
                    if self._power_push_revision > power_push_revision
                    else confirmed_is_on
                )
                if not published_is_on:
                    self._last_fan_mode = None
                self._last_published_command_revision = command_revision
                self._publish_data(
                    replace(
                        current,
                        is_on=published_is_on,
                        fan_mode=current.fan_mode if published_is_on else None,
                    )
                )
        self._schedule_background_refresh()

    async def async_set_fan_mode(self, mode: str) -> None:
        """Set fan mode, powering on first if needed."""

        if mode not in self.profile.fan_mode_commands:
            raise ValueError(f"Unsupported fan mode: {mode}")
        self._cancel_background_refresh()
        self._command_revision += 1
        command_revision = self._command_revision
        power_push_revision = self._power_push_revision
        fan_mode_push_revision = self._fan_mode_push_revision
        async with self._command_lock:
            current_before_command = self.data
            if current_before_command is None or current_before_command.is_on is False:
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
                confirmed_is_on = current_before_command.is_on
                confirmed_mode = mode if mode_result is None else mode_result
            async with self._state_lock:
                current = self.data or PurifierState()
                published_is_on = (
                    current.is_on
                    if self._power_push_revision > power_push_revision
                    else confirmed_is_on
                )
                published_mode = (
                    current.fan_mode
                    if self._fan_mode_push_revision > fan_mode_push_revision
                    else confirmed_mode
                )
                self._last_fan_mode = published_mode
                self._last_published_command_revision = command_revision
                self._publish_data(
                    replace(
                        current,
                        is_on=published_is_on,
                        fan_mode=published_mode,
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
        self._command_revision += 1
        command_revision = self._command_revision
        night_light_push_revision = self._night_light_push_revision

        confirmed_any = False
        try:
            async with self._command_lock:
                current = self.data or PurifierState(night_light=NightLightState())
                night_light = current.night_light or NightLightState()
                has_settings = brightness_percent is not None or rgb_color is not None
                should_set_power = not is_on or not has_settings or night_light.is_on is not True

                if should_set_power:
                    update = await self.client.async_set_night_light_power(is_on)
                    night_light = _merge_night_light_state(night_light, update)
                    async with self._state_lock:
                        current = self.data or current
                        if (
                            self._night_light_push_revision
                            <= night_light_push_revision
                        ):
                            current = replace(current, night_light=night_light)
                        else:
                            night_light = current.night_light or NightLightState()
                        self._last_published_command_revision = command_revision
                        self._publish_data(current)
                    confirmed_any = True

                if is_on and brightness_percent is not None:
                    update = await self.client.async_set_night_light_brightness(
                        brightness_percent
                    )
                    night_light = _merge_night_light_state(night_light, update)
                    async with self._state_lock:
                        current = self.data or current
                        if (
                            self._night_light_push_revision
                            <= night_light_push_revision
                        ):
                            current = replace(current, night_light=night_light)
                        else:
                            night_light = current.night_light or NightLightState()
                        self._last_published_command_revision = command_revision
                        self._publish_data(current)
                    confirmed_any = True

                if is_on and rgb_color is not None:
                    update = await self.client.async_set_night_light_rgb(rgb_color)
                    night_light = _merge_night_light_state(night_light, update)
                    async with self._state_lock:
                        current = self.data or current
                        if (
                            self._night_light_push_revision
                            <= night_light_push_revision
                        ):
                            current = replace(current, night_light=night_light)
                        else:
                            night_light = current.night_light or NightLightState()
                        self._last_published_command_revision = command_revision
                        self._publish_data(current)
                    confirmed_any = True
        finally:
            if confirmed_any:
                self._schedule_background_refresh()
