"""Remember and resume integration-known automatic fan modes."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging
from typing import Any, Literal

from .custom_auto.policy import CUSTOM_AUTO_SPEEDS

AUTO_MODE_HARDWARE = "hardware_auto"
AUTO_MODE_CUSTOM = "custom_auto"
ATTR_AUTO_RESUME_MODE = "auto_resume_mode"
ATTR_AUTO_RESUME_SUSPENDED = "auto_resume_suspended"
ATTR_AUTO_RESUME_CUSTOM_SPEED = "auto_resume_custom_speed"
AutoMode = Literal["hardware_auto", "custom_auto"]

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AutoResumeState:
    """Stable automatic-mode intent persisted by the fan entity."""

    mode: AutoMode | None = None
    suspended: bool = False
    custom_speed: int | None = None


class AutoResumeManager:
    """Serialize automatic-mode selection, suspension, and resumption."""

    def __init__(
        self,
        hass: Any,
        coordinator: Any,
        controller: Any,
        *,
        config_entry: Any = None,
    ) -> None:
        self._hass = hass
        self.coordinator = coordinator
        self.controller = controller
        self._config_entry = config_entry
        self._state = AutoResumeState()
        self._listeners: set[Callable[[], None]] = set()
        self._lock = asyncio.Lock()
        self._last_device_observation_revision = getattr(
            coordinator, "device_observation_revision", coordinator.poll_revision
        )
        self._reconcile_pending = False
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._stopped = False
        self._remove_coordinator_listener = coordinator.async_add_listener(
            self._handle_coordinator_update
        )

    @property
    def state(self) -> AutoResumeState:
        """Return the current stable automatic-mode intent."""

        return self._state

    @property
    def custom_speed(self) -> int | None:
        """Return the current or remembered Custom Auto speed."""

        if self._state.mode != AUTO_MODE_CUSTOM:
            return None
        current_speed = self.controller.current_speed
        if self.controller.active and current_speed in CUSTOM_AUTO_SPEEDS:
            return current_speed
        return self._state.custom_speed

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe to stable automatic-mode intent changes."""

        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    def diagnostics(self) -> dict[str, str | bool | int | None]:
        """Return non-sensitive remembered automatic-mode details."""

        return {
            "mode": self._state.mode,
            "suspended": self._state.suspended,
            "custom_speed": self.custom_speed,
            "reconcile_pending": self._reconcile_pending,
        }

    async def async_restore(
        self,
        mode: str | None,
        *,
        suspended: bool,
        custom_speed: int | None = None,
    ) -> None:
        """Restore persisted intent without powering on an off purifier."""

        state = self._validated_state(mode, suspended, custom_speed)
        should_reconcile = False
        async with self._lock:
            is_on = getattr(self.coordinator.data, "is_on", None)
            if state.mode is not None and is_on is not True:
                state = AutoResumeState(
                    mode=state.mode,
                    suspended=True,
                    custom_speed=state.custom_speed,
                )
            self._set_state(state)
            should_reconcile = bool(
                state.mode is not None
                and is_on is True
                and (
                    state.suspended
                    or (
                        state.mode == AUTO_MODE_CUSTOM
                        and not self.controller.active
                    )
                )
            )
        if should_reconcile:
            self._schedule_reconcile()

    async def async_restore_from_hass(self, unique_id: str) -> None:
        """Restore the newest fan or Custom Auto entity record."""

        if self._hass is None:
            return
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
        from homeassistant.helpers import restore_state  # noqa: PLC0415

        registry = er.async_get(self._hass)
        last_states = restore_state.async_get(self._hass).last_states
        candidates: list[tuple[float, int, AutoResumeState]] = []
        for domain, suffix in (("fan", "fan"), ("switch", "custom_auto")):
            entity_id = registry.async_get_entity_id(
                domain,
                "govee_ble_air_purifier",
                f"{unique_id}_{suffix}",
            )
            if entity_id is None or (stored := last_states.get(entity_id)) is None:
                continue
            restored = self._state_from_restore_record(stored.state, domain)
            if restored is None:
                continue
            state, schema_version = restored
            last_updated = getattr(stored.state, "last_updated", None)
            timestamp = (
                last_updated.timestamp()
                if last_updated is not None
                and hasattr(last_updated, "timestamp")
                else 0.0
            )
            candidates.append((timestamp, schema_version, state))

        if not candidates:
            return
        _timestamp, _schema_version, state = max(
            candidates, key=lambda candidate: (candidate[0], candidate[1])
        )
        await self.async_restore(
            state.mode,
            suspended=state.suspended,
            custom_speed=state.custom_speed,
        )

    async def async_turn_on(self) -> None:
        """Turn on the purifier and resume any suspended automatic mode."""

        async with self._lock:
            state = self._state
            is_on = getattr(self.coordinator.data, "is_on", None)
            custom_needs_activation = bool(
                state.mode == AUTO_MODE_CUSTOM and not self.controller.active
            )
            if state.mode is not None and (
                state.suspended or is_on is False or custom_needs_activation
            ):
                if is_on is False and not state.suspended:
                    state = AutoResumeState(
                        mode=state.mode,
                        suspended=True,
                        custom_speed=self.custom_speed,
                    )
                    self._set_state(state)
                await self._async_resume_locked(
                    force=state.suspended or is_on is False,
                    restoring=False,
                )
                return
            if is_on is True:
                return
            await self.coordinator.async_set_power(True)

    async def async_turn_off(self) -> None:
        """Turn off the purifier while retaining selected automatic intent."""

        async with self._lock:
            state = self._state
            if self.controller.active:
                state = AutoResumeState(
                    mode=AUTO_MODE_CUSTOM,
                    custom_speed=self.custom_speed,
                )
                await self.controller.async_handoff(
                    lambda: self.coordinator.async_set_power(False)
                )
            else:
                if (
                    state.mode is None
                    and getattr(self.coordinator.data, "fan_mode", None) == "Auto"
                ):
                    state = AutoResumeState(mode=AUTO_MODE_HARDWARE)
                await self.coordinator.async_set_power(False)

            if state.mode is None:
                self._set_state(AutoResumeState())
            else:
                self._set_state(
                    AutoResumeState(
                        mode=state.mode,
                        suspended=True,
                        custom_speed=(
                            self.controller.current_speed
                            if state.mode == AUTO_MODE_CUSTOM
                            and self.controller.current_speed in CUSTOM_AUTO_SPEEDS
                            else state.custom_speed
                        ),
                    )
                )

    async def async_set_manual_mode(self, mode: str) -> None:
        """Set a manual mode and clear remembered automatic intent."""

        async with self._lock:
            await self._async_handoff_locked(
                lambda: self.coordinator.async_set_fan_mode(mode)
            )
            self._set_state(AutoResumeState())

    async def async_set_hardware_auto(self) -> None:
        """Select and remember the purifier's built-in Auto mode."""

        async with self._lock:
            await self._async_handoff_locked(
                lambda: self.coordinator.async_set_fan_mode("Auto")
            )
            self._set_state(AutoResumeState(mode=AUTO_MODE_HARDWARE))

    async def async_enable_custom_auto(self) -> None:
        """Activate and remember integration-managed Custom Auto."""

        async with self._lock:
            if not self.controller.active:
                await self.controller.async_activate()
            self._set_state(
                AutoResumeState(
                    mode=AUTO_MODE_CUSTOM,
                    custom_speed=self.controller.current_speed,
                )
            )

    async def async_disable_custom_auto(self) -> None:
        """Replace Custom Auto with hardware Auto without losing off state."""

        async with self._lock:
            state = self._state
            if state.mode != AUTO_MODE_CUSTOM and not self.controller.active:
                return
            if (
                state.suspended
                and not self.controller.active
                and getattr(self.coordinator.data, "is_on", None) is not True
            ):
                self._set_state(
                    AutoResumeState(
                        mode=AUTO_MODE_HARDWARE,
                        suspended=True,
                    )
                )
                return
            await self._async_handoff_locked(
                lambda: self.coordinator.async_set_fan_mode("Auto")
            )
            self._set_state(AutoResumeState(mode=AUTO_MODE_HARDWARE))

    async def async_handle_physical_fan_mode(self, mode: str) -> None:
        """Accept a physical mode selection as the newest user intent."""

        async with self._lock:
            if self.controller.active:
                await self.controller.async_deactivate()
            if mode == "Auto":
                self._set_state(AutoResumeState(mode=AUTO_MODE_HARDWARE))
            else:
                self._set_state(AutoResumeState())

    async def async_stop(self) -> None:
        """Release coordinator listeners and pending reconciliation work."""

        self._stopped = True
        self._remove_coordinator_listener()
        task = self._reconcile_task
        self._reconcile_task = None
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._listeners.clear()

    async def _async_handoff_locked(
        self, command: Callable[[], Awaitable[None]]
    ) -> None:
        if self.controller.active:
            await self.controller.async_handoff(command)
            return
        await command()

    async def _async_resume_locked(self, *, force: bool, restoring: bool) -> None:
        state = self._state
        if state.mode == AUTO_MODE_HARDWARE:
            await self.coordinator.async_set_fan_mode("Auto")
            self._set_state(AutoResumeState(mode=AUTO_MODE_HARDWARE))
            return
        if state.mode == AUTO_MODE_CUSTOM:
            await self.controller.async_activate(
                restored_speed=state.custom_speed,
                restoring=restoring,
                force=force,
            )
            self._set_state(
                AutoResumeState(
                    mode=AUTO_MODE_CUSTOM,
                    custom_speed=self.controller.current_speed,
                )
            )

    def _handle_coordinator_update(self) -> None:
        """Capture Custom Auto speed and react only to fresh BLE polls."""

        self._sync_custom_speed()
        revision = getattr(
            self.coordinator,
            "device_observation_revision",
            self.coordinator.poll_revision,
        )
        if revision == self._last_device_observation_revision:
            return
        self._last_device_observation_revision = revision
        self._schedule_reconcile()

    def _schedule_reconcile(self) -> None:
        if self._stopped:
            return
        self._reconcile_pending = True
        if self._reconcile_task is None or self._reconcile_task.done():
            self._reconcile_task = self._create_task(self._async_run_reconcile())

    async def _async_run_reconcile(self) -> None:
        try:
            while self._reconcile_pending and not self._stopped:
                self._reconcile_pending = False
                try:
                    await self._async_reconcile_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    LOGGER.exception("Failed to reconcile remembered Auto mode")
        finally:
            if self._reconcile_task is asyncio.current_task():
                self._reconcile_task = None

    async def _async_reconcile_once(self) -> None:
        async with self._lock:
            state = self._state
            if state.mode is None:
                return
            is_on = getattr(self.coordinator.data, "is_on", None)
            if is_on is False:
                speed = self.custom_speed
                if state.mode == AUTO_MODE_CUSTOM and self.controller.active:
                    await self.controller.async_deactivate()
                self._set_state(
                    AutoResumeState(
                        mode=state.mode,
                        suspended=True,
                        custom_speed=speed,
                    )
                )
                return
            if is_on is not True:
                return
            if state.suspended:
                await self._async_resume_locked(force=True, restoring=True)
                return
            if state.mode == AUTO_MODE_CUSTOM and not self.controller.active:
                await self._async_resume_locked(force=False, restoring=True)

    def _sync_custom_speed(self) -> None:
        state = self._state
        speed = self.controller.current_speed
        if (
            state.mode == AUTO_MODE_CUSTOM
            and self.controller.active
            and speed in CUSTOM_AUTO_SPEEDS
            and speed != state.custom_speed
        ):
            self._set_state(
                AutoResumeState(
                    mode=AUTO_MODE_CUSTOM,
                    suspended=state.suspended,
                    custom_speed=speed,
                )
            )

    def _validated_state(
        self, mode: str | None, suspended: bool, custom_speed: int | None
    ) -> AutoResumeState:
        if mode == AUTO_MODE_HARDWARE:
            if "Auto" not in self.coordinator.profile.fan_mode_commands:
                return AutoResumeState()
            return AutoResumeState(mode=AUTO_MODE_HARDWARE, suspended=suspended)
        if mode == AUTO_MODE_CUSTOM:
            if not self.coordinator.profile.supports_custom_auto:
                return AutoResumeState()
            return AutoResumeState(
                mode=AUTO_MODE_CUSTOM,
                suspended=suspended,
                custom_speed=(
                    custom_speed if custom_speed in CUSTOM_AUTO_SPEEDS else None
                ),
            )
        return AutoResumeState()

    def _state_from_restore_record(
        self, state: Any, domain: str
    ) -> tuple[AutoResumeState, int] | None:
        attributes = state.attributes
        if ATTR_AUTO_RESUME_MODE in attributes:
            return (
                self._validated_state(
                    attributes.get(ATTR_AUTO_RESUME_MODE),
                    attributes.get(ATTR_AUTO_RESUME_SUSPENDED) is True,
                    attributes.get(ATTR_AUTO_RESUME_CUSTOM_SPEED),
                ),
                1,
            )
        if "custom_auto_active" not in attributes and domain != "switch":
            return None
        custom_active = attributes.get("custom_auto_active") is True
        if domain == "switch":
            custom_active = custom_active or state.state == "on"
        return (
            self._validated_state(
                AUTO_MODE_CUSTOM if custom_active else None,
                domain == "fan" and state.state != "on",
                attributes.get("custom_auto_speed"),
            ),
            0,
        )

    def _set_state(self, state: AutoResumeState) -> None:
        if state == self._state:
            return
        self._state = state
        for listener in tuple(self._listeners):
            try:
                listener()
            except Exception:
                LOGGER.exception("Auto resume state listener failed")

    def _create_task(self, coroutine: Awaitable[Any]) -> asyncio.Task[Any]:
        if self._config_entry is not None and hasattr(
            self._config_entry, "async_create_background_task"
        ):
            return self._config_entry.async_create_background_task(
                self._hass,
                coroutine,
                "Govee BLE Air Purifier Auto resume",
            )
        if self._hass is not None and hasattr(self._hass, "async_create_task"):
            return self._hass.async_create_task(
                coroutine, "Govee BLE Air Purifier Auto resume"
            )
        return asyncio.create_task(coroutine)
