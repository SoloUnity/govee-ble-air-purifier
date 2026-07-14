"""Integration-managed PM2.5 automatic fan speed control."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
import logging
from typing import Any

from .const import (
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
    CONF_CUSTOM_AUTO_DOWN_20,
    CONF_CUSTOM_AUTO_DOWN_40,
    CONF_CUSTOM_AUTO_DOWN_60,
    CONF_CUSTOM_AUTO_DOWN_80,
    CONF_CUSTOM_AUTO_UP_100,
    CONF_CUSTOM_AUTO_UP_40,
    CONF_CUSTOM_AUTO_UP_60,
    CONF_CUSTOM_AUTO_UP_80,
)

CUSTOM_AUTO_SPEEDS = (20, 40, 60, 80, 100)
SPEED_TO_MODE = {
    20: "Sleep",
    40: "Low",
    60: "Medium",
    80: "High",
    100: "Turbo",
}
MODE_TO_SPEED = {mode: speed for speed, mode in SPEED_TO_MODE.items()}
LOGGER = logging.getLogger(__name__)

CUSTOM_AUTO_DEFAULTS: dict[str, int] = {
    CONF_CUSTOM_AUTO_UP_40: 3,
    CONF_CUSTOM_AUTO_UP_60: 5,
    CONF_CUSTOM_AUTO_UP_80: 9,
    CONF_CUSTOM_AUTO_UP_100: 15,
    CONF_CUSTOM_AUTO_DOWN_80: 14,
    CONF_CUSTOM_AUTO_DELAY_80: 5,
    CONF_CUSTOM_AUTO_DOWN_60: 9,
    CONF_CUSTOM_AUTO_DELAY_60: 5,
    CONF_CUSTOM_AUTO_DOWN_40: 5,
    CONF_CUSTOM_AUTO_DELAY_40: 5,
    CONF_CUSTOM_AUTO_DOWN_20: 3,
    CONF_CUSTOM_AUTO_DELAY_20: 7,
}

UP_THRESHOLD_KEYS = (
    CONF_CUSTOM_AUTO_UP_40,
    CONF_CUSTOM_AUTO_UP_60,
    CONF_CUSTOM_AUTO_UP_80,
    CONF_CUSTOM_AUTO_UP_100,
)
DOWN_THRESHOLD_KEYS = (
    CONF_CUSTOM_AUTO_DOWN_20,
    CONF_CUSTOM_AUTO_DOWN_40,
    CONF_CUSTOM_AUTO_DOWN_60,
    CONF_CUSTOM_AUTO_DOWN_80,
)
DOWN_DELAY_KEYS = (
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
)
CUSTOM_AUTO_OPTION_KEYS = UP_THRESHOLD_KEYS + DOWN_THRESHOLD_KEYS + DOWN_DELAY_KEYS


@dataclass(frozen=True)
class CustomAutoConfig:
    """Validated custom-auto configuration."""

    up_thresholds: tuple[int, int, int, int]
    down_thresholds: tuple[int, int, int, int]
    down_delays: tuple[int, int, int, int]

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "CustomAutoConfig":
        """Read options, falling back safely for old or malformed entries."""

        try:
            values = parse_custom_auto_values(options)
            validate_custom_auto_values(values)
        except ValueError:
            values = dict(CUSTOM_AUTO_DEFAULTS)
        return cls(
            up_thresholds=tuple(values[key] for key in UP_THRESHOLD_KEYS),
            down_thresholds=tuple(values[key] for key in DOWN_THRESHOLD_KEYS),
            down_delays=tuple(values[key] for key in DOWN_DELAY_KEYS),
        )

    def as_options(self) -> dict[str, int]:
        """Return the configuration in config-entry option form."""

        values: dict[str, int] = {}
        values.update(dict(zip(UP_THRESHOLD_KEYS, self.up_thresholds, strict=True)))
        values.update(
            dict(zip(DOWN_THRESHOLD_KEYS, self.down_thresholds, strict=True))
        )
        values.update(dict(zip(DOWN_DELAY_KEYS, self.down_delays, strict=True)))
        return values


def parse_custom_auto_values(values: Mapping[str, Any]) -> dict[str, int]:
    """Parse bounded integer rule values, applying defaults for missing fields."""

    parsed: dict[str, int] = {}
    for key in CUSTOM_AUTO_OPTION_KEYS:
        value = values.get(key, CUSTOM_AUTO_DEFAULTS[key])
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{key} must be an integer") from err
        if number != value and not (isinstance(value, str) and str(number) == value):
            raise ValueError(f"{key} must be an integer")
        maximum = 1440 if key in DOWN_DELAY_KEYS else 999
        if not 0 <= number <= maximum:
            raise ValueError(f"{key} is outside its allowed range")
        parsed[key] = number
    return parsed


def validate_custom_auto_values(values: Mapping[str, int]) -> None:
    """Validate threshold ordering and hysteresis relationships."""

    up = tuple(values[key] for key in UP_THRESHOLD_KEYS)
    down = tuple(values[key] for key in DOWN_THRESHOLD_KEYS)
    if not all(left < right for left, right in zip(up, up[1:])):
        raise ValueError("up_thresholds_not_ascending")
    if not all(left < right for left, right in zip(down, down[1:])):
        raise ValueError("down_thresholds_not_ascending")
    if any(down_value > up_value for down_value, up_value in zip(down, up)):
        raise ValueError("down_threshold_above_up")


class CustomAutoController:
    """Apply custom-auto speed rules to coordinator PM2.5 updates."""

    def __init__(
        self,
        hass: Any,
        coordinator: Any,
        config: CustomAutoConfig,
        *,
        config_entry: Any = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self._hass = hass
        self.coordinator = coordinator
        self.config = config
        self._config_entry = config_entry
        self._sleep = sleep
        self._active = False
        self._current_speed: int | None = None
        self._remove_listener: Callable[[], None] | None = None
        self._timer_tasks: dict[int, asyncio.Task[Any]] = {}
        self._mature_downshifts: set[int] = set()
        self._evaluation_task: asyncio.Task[Any] | None = None
        self._evaluation_pending = False
        self._waiting_for_successful_update = False
        self._state_listeners: set[Callable[[], None]] = set()
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        """Return whether custom auto currently owns fan speed."""

        return self._active

    @property
    def current_speed(self) -> int | None:
        """Return the last custom-auto manual percentage."""

        return self._current_speed

    @property
    def pending_downshifts(self) -> tuple[int, ...]:
        """Return target speeds whose conditions currently have timers."""

        return tuple(sorted(self._timer_tasks))

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to controller ownership changes."""

        self._state_listeners.add(listener)
        return lambda: self._state_listeners.discard(listener)

    async def async_activate(
        self, *, restored_speed: int | None = None, restoring: bool = False
    ) -> None:
        """Activate custom auto and establish or restore its manual speed."""

        timer_tasks: tuple[asyncio.Task[Any], ...] = ()
        async with self._lock:
            previous_speed = self._current_speed
            already_active = self._active
            try:
                if already_active:
                    if (
                        self._coordinator_update_succeeded()
                        and getattr(self.coordinator.data, "is_on", None) is not False
                        and (pm25 := getattr(self.coordinator.data, "pm25", None))
                        is not None
                        and self._current_speed is not None
                    ):
                        required_speed = self._speed_for_pm(pm25)
                        if required_speed > self._current_speed:
                            await self._async_set_speed(required_speed)
                    return

                self._active = True
                self._waiting_for_successful_update = False
                self._remove_listener = self.coordinator.async_add_listener(
                    self._handle_coordinator_update
                )
                if restored_speed in CUSTOM_AUTO_SPEEDS:
                    self._current_speed = restored_speed
                elif self._current_speed not in CUSTOM_AUTO_SPEEDS:
                    mode = getattr(self.coordinator.data, "fan_mode", None)
                    self._current_speed = MODE_TO_SPEED.get(
                        mode, CUSTOM_AUTO_SPEEDS[0]
                    )

                update_succeeded = self._coordinator_update_succeeded()
                self._waiting_for_successful_update = not update_succeeded
                pm25 = (
                    getattr(self.coordinator.data, "pm25", None)
                    if update_succeeded
                    else None
                )
                if pm25 is None:
                    await self._async_set_speed(self._current_speed)
                else:
                    required_speed = self._speed_for_pm(pm25)
                    if not restoring or self._current_speed is None:
                        await self._async_set_speed(required_speed)
                    elif required_speed > self._current_speed:
                        await self._async_set_speed(required_speed)
                    else:
                        await self._async_set_speed(self._current_speed)
                    if update_succeeded:
                        self._update_downshift_timers(pm25)
                self._notify_state_listeners()
            except BaseException:
                self._current_speed = previous_speed
                timer_tasks = self._deactivate_locked()
                raise
            finally:
                if timer_tasks:
                    await asyncio.gather(*timer_tasks, return_exceptions=True)

    async def async_deactivate(self) -> None:
        """Stop custom auto without issuing a purifier command."""

        async with self._lock:
            timer_tasks = self._deactivate_locked()
        if timer_tasks:
            await asyncio.gather(*timer_tasks, return_exceptions=True)

    async def async_handoff(self, command: Callable[[], Awaitable[None]]) -> None:
        """Run a user command and release ownership only after it succeeds."""

        timer_tasks: tuple[asyncio.Task[Any], ...] = ()
        async with self._lock:
            was_active = self._active
            previous_speed = self._current_speed
            try:
                await command()
            except BaseException:
                if was_active and previous_speed in CUSTOM_AUTO_SPEEDS:
                    try:
                        await self.coordinator.async_set_fan_mode(
                            SPEED_TO_MODE[previous_speed]
                        )
                    except Exception:
                        LOGGER.exception(
                            "Failed to reassert Custom Auto speed after handoff failure"
                        )
                raise
            if was_active:
                timer_tasks = self._deactivate_locked()
        if timer_tasks:
            await asyncio.gather(*timer_tasks, return_exceptions=True)

    async def async_stop(self) -> None:
        """Release all listeners and tasks during config-entry unload."""

        await self.async_deactivate()
        task = self._evaluation_task
        self._evaluation_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def diagnostics(self) -> dict[str, Any]:
        """Return non-sensitive configured and runtime controller details."""

        return {
            "active": self.active,
            "current_speed": self.current_speed,
            "up_thresholds": list(self.config.up_thresholds),
            "down_thresholds": list(self.config.down_thresholds),
            "down_delays": list(self.config.down_delays),
            "pending_downshifts": list(self.pending_downshifts),
            "mature_downshifts": sorted(self._mature_downshifts),
        }

    def _handle_coordinator_update(self) -> None:
        """Coalesce coordinator callbacks into one serialized evaluator."""

        if not self._active:
            return
        if self._waiting_for_successful_update:
            if self._coordinator_update_succeeded():
                self._waiting_for_successful_update = False
            elif getattr(self.coordinator.data, "is_on", None) is not False:
                return
        self._schedule_evaluation()

    def _schedule_evaluation(self) -> None:
        """Schedule evaluation without treating it as a fresh data update."""

        self._evaluation_pending = True
        if self._evaluation_task is None or self._evaluation_task.done():
            self._evaluation_task = self._create_task(self._async_run_evaluations())

    async def _async_run_evaluations(self) -> None:
        try:
            while self._evaluation_pending and self._active:
                self._evaluation_pending = False
                async with self._lock:
                    if not self._active:
                        return
                    await self._async_evaluate()
        except asyncio.CancelledError:
            raise
        except Exception:
            LOGGER.exception("Custom auto evaluation failed")
            self._evaluation_pending = False
            async with self._lock:
                if self._active:
                    self._waiting_for_successful_update = True
        finally:
            self._evaluation_task = None

    async def _async_evaluate(self) -> None:
        data = self.coordinator.data
        if getattr(data, "is_on", None) is False:
            timer_tasks = self._deactivate_locked()
            if timer_tasks:
                await asyncio.gather(*timer_tasks, return_exceptions=True)
            return
        if not self._coordinator_update_succeeded():
            return
        if self._waiting_for_successful_update:
            return

        pm25 = getattr(data, "pm25", None)
        if pm25 is None or self._current_speed is None:
            return

        required_speed = self._speed_for_pm(pm25)
        self._update_downshift_timers(pm25)
        if required_speed > self._current_speed:
            await self._async_set_speed(required_speed)

        eligible = [
            speed
            for speed in self._mature_downshifts
            if self._current_speed is not None and speed < self._current_speed
        ]
        if eligible:
            await self._async_set_speed(min(eligible))

    def _speed_for_pm(self, pm25: int) -> int:
        speed = CUSTOM_AUTO_SPEEDS[0]
        for threshold, candidate in zip(
            self.config.up_thresholds, CUSTOM_AUTO_SPEEDS[1:], strict=True
        ):
            if pm25 > threshold:
                speed = candidate
        return speed

    async def _async_set_speed(self, speed: int) -> None:
        if speed == self._current_speed:
            mode = getattr(self.coordinator.data, "fan_mode", None)
            is_on = getattr(self.coordinator.data, "is_on", None)
            if mode == SPEED_TO_MODE[speed] and is_on is not False:
                return
        previous_speed = self._current_speed
        self._current_speed = speed
        try:
            await self.coordinator.async_set_fan_mode(SPEED_TO_MODE[speed])
        except BaseException:
            self._current_speed = previous_speed
            raise

    def _update_downshift_timers(self, pm25: int) -> None:
        for speed, threshold, delay in zip(
            CUSTOM_AUTO_SPEEDS[:-1],
            self.config.down_thresholds,
            self.config.down_delays,
            strict=True,
        ):
            if pm25 <= threshold:
                if speed not in self._timer_tasks and speed not in self._mature_downshifts:
                    self._timer_tasks[speed] = self._create_task(
                        self._async_downshift_timer(speed, delay)
                    )
            else:
                task = self._timer_tasks.pop(speed, None)
                if task is not None and not task.done():
                    task.cancel()
                self._mature_downshifts.discard(speed)

    async def _async_downshift_timer(self, speed: int, delay_minutes: int) -> None:
        try:
            await self._sleep(delay_minutes * 60)
        except asyncio.CancelledError:
            raise
        else:
            self._timer_tasks.pop(speed, None)
            if (
                self._active
                and getattr(self.coordinator.data, "is_on", None) is not False
            ):
                self._mature_downshifts.add(speed)
                if (
                    self._coordinator_update_succeeded()
                    and not self._waiting_for_successful_update
                ):
                    self._schedule_evaluation()

    def _deactivate_locked(self) -> tuple[asyncio.Task[Any], ...]:
        """Release controller ownership while the evaluation lock is held."""

        was_active = self._active
        self._active = False
        self._waiting_for_successful_update = False
        self._evaluation_pending = False
        if self._remove_listener is not None:
            self._remove_listener()
            self._remove_listener = None
        timer_tasks = self._cancel_downshift_timers()
        self._mature_downshifts.clear()
        if was_active:
            self._notify_state_listeners()
        return timer_tasks

    def _coordinator_update_succeeded(self) -> bool:
        """Treat lightweight coordinators without availability state as successful."""

        if hasattr(self.coordinator, "last_pm25_update_success"):
            return self.coordinator.last_pm25_update_success is not False
        if hasattr(self.coordinator, "last_poll_success"):
            return self.coordinator.last_poll_success is not False
        return getattr(self.coordinator, "last_update_success", True) is not False

    def _notify_state_listeners(self) -> None:
        """Notify entities that logical controller state changed."""

        for listener in tuple(self._state_listeners):
            try:
                listener()
            except Exception:
                LOGGER.exception("Custom auto state listener failed")

    def _cancel_downshift_timers(self) -> tuple[asyncio.Task[Any], ...]:
        tasks = tuple(self._timer_tasks.values())
        for task in tasks:
            if not task.done():
                task.cancel()
        self._timer_tasks.clear()
        return tasks

    def _create_task(self, coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
        if self._config_entry is not None and hasattr(
            self._config_entry, "async_create_background_task"
        ):
            return self._config_entry.async_create_background_task(
                self._hass,
                coroutine,
                "Govee BLE Air Purifier custom auto",
            )
        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            return self._hass.async_create_task(
                coroutine, "Govee BLE Air Purifier custom auto"
            )
        return asyncio.create_task(coroutine)
