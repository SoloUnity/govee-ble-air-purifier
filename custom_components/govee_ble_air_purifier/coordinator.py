"""Data coordinator for Govee BLE air purifiers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
import logging
from typing import Any

from .const import DEFAULT_POLLING_INTERVAL_SECONDS
from .profiles import H7124_PROFILE, ModelProfile

POLLING_INTERVAL = timedelta(seconds=DEFAULT_POLLING_INTERVAL_SECONDS)
LOGGER = logging.getLogger(__name__)

try:  # pragma: no cover - exercised in Home Assistant, not pure unit tests
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
except ModuleNotFoundError:  # pragma: no cover - fallback keeps pure tests lightweight
    DataUpdateCoordinator = object  # type: ignore[assignment]

    class UpdateFailed(Exception):
        """Fallback UpdateFailed for tests without Home Assistant installed."""


@dataclass(frozen=True)
class GoveeData:
    """State shared by all Home Assistant entities."""

    is_on: bool | None = None
    pm25: int | None = None
    filter_life: int | None = None
    fan_mode: str | None = None


@dataclass(frozen=True)
class GoveeRuntimeData:
    """Runtime objects attached to a Home Assistant config entry."""

    coordinator: "GoveeCoordinator"
    profile: ModelProfile


class GoveeCoordinator(DataUpdateCoordinator):  # type: ignore[misc]
    """Coordinate BLE polling and command-side refreshes."""

    def __init__(
        self,
        hass: Any,
        client: Any,
        *,
        profile: ModelProfile = H7124_PROFILE,
        polling_interval: timedelta = POLLING_INTERVAL,
        update_method_only: bool = False,
    ) -> None:
        self._hass = hass
        self.client = client
        self.profile = profile
        self.polling_interval = polling_interval
        self.data: GoveeData | None = None
        self._last_fan_mode: str | None = None
        self._background_refresh_task: asyncio.Task[Any] | None = None
        self._standalone = update_method_only or DataUpdateCoordinator is object
        if not self._standalone:
            super().__init__(
                hass,
                LOGGER,
                name="Govee BLE Air Purifier",
                update_interval=polling_interval,
            )

    def _publish_data(self, data: GoveeData) -> None:
        """Publish coordinator data to subscribed entities immediately."""

        if not self._standalone and hasattr(self, "async_set_updated_data"):
            self.async_set_updated_data(data)  # type: ignore[attr-defined]
            return
        self.data = data

    def _schedule_background_refresh(self) -> None:
        """Refresh later without blocking command UI updates."""

        if self._standalone:
            return

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

    def _cancel_background_refresh(self) -> None:
        """Cancel a scheduled refresh so commands can use BLE first."""

        task = self._background_refresh_task
        if task is not None and not task.done():
            task.cancel()
        self._background_refresh_task = None

    async def _async_update_data(self) -> GoveeData:
        """Fetch current state from the BLE client."""

        try:
            client_data = await self.client.async_get_state()
        except Exception as err:  # pragma: no cover - depends on HA runtime
            raise UpdateFailed(str(err)) from err
        data = GoveeData(
            is_on=client_data.is_on,
            pm25=client_data.pm25,
            filter_life=client_data.filter_life,
            fan_mode=self._last_fan_mode or client_data.fan_mode,
        )
        self.data = data
        return data

    async def async_request_refresh(self) -> None:
        """Refresh data in standalone tests or delegate to HA in production."""

        if self._standalone:
            await self._async_update_data()
            return
        await super().async_request_refresh()  # type: ignore[misc]

    async def async_set_power(self, is_on: bool) -> None:
        """Set power and refresh shared state."""

        self._cancel_background_refresh()
        result = await self.client.async_set_power(is_on)
        confirmed_is_on = is_on if result is None else result
        if not confirmed_is_on:
            self._last_fan_mode = None
        current = self.data or GoveeData()
        self._publish_data(
            GoveeData(
                is_on=confirmed_is_on,
                pm25=current.pm25,
                filter_life=current.filter_life,
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
        if self.data is not None and self.data.is_on is False:
            if hasattr(self.client, "async_set_power_and_fan_mode"):
                result = await self.client.async_set_power_and_fan_mode(mode)
                confirmed_is_on = (
                    result.is_on
                    if isinstance(result, GoveeData) and result.is_on is not None
                    else True
                )
                confirmed_mode = (
                    result.fan_mode
                    if isinstance(result, GoveeData) and result.fan_mode is not None
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
        current = self.data or GoveeData()
        self._publish_data(
            GoveeData(
                is_on=confirmed_is_on,
                pm25=current.pm25,
                filter_life=current.filter_life,
                fan_mode=confirmed_mode,
            )
        )
        self._schedule_background_refresh()
