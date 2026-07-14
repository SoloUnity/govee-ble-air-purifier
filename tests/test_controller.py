import asyncio
from collections.abc import Callable

import pytest

from custom_components.govee_ble_air_purifier.controller import (
    CUSTOM_AUTO_DEFAULTS,
    CustomAutoConfig,
    CustomAutoController,
)
from custom_components.govee_ble_air_purifier.coordinator import GoveeData


class FakeCoordinator:
    def __init__(
        self, *, pm25: int | None, mode: str = "Sleep", is_on: bool = True
    ) -> None:
        self.data = GoveeData(
            is_on=is_on, pm25=pm25, filter_life=90, fan_mode=mode
        )
        self.last_update_success = True
        self.last_pm25_update_success = pm25 is not None
        self.commands: list[str] = []
        self.command_attempts: list[str] = []
        self.command_errors: list[Exception] = []
        self.listeners: list[Callable[[], None]] = []

    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        self.listeners.append(listener)

        def remove() -> None:
            self.listeners.remove(listener)

        return remove

    async def async_set_fan_mode(self, mode: str) -> None:
        self.command_attempts.append(mode)
        if self.command_errors:
            raise self.command_errors.pop(0)
        self.commands.append(mode)
        self.data = GoveeData(
            is_on=True,
            pm25=self.data.pm25,
            filter_life=self.data.filter_life,
            fan_mode=mode,
        )
        self._notify()

    def set_is_on(self, is_on: bool) -> None:
        self.data = GoveeData(
            is_on=is_on,
            pm25=self.data.pm25,
            filter_life=self.data.filter_life,
            fan_mode=self.data.fan_mode if is_on else None,
        )
        self._notify()

    def set_update_success(self, successful: bool) -> None:
        self.last_update_success = successful
        self.last_pm25_update_success = successful
        self._notify()

    def set_pm25(self, pm25: int | None) -> None:
        self.last_pm25_update_success = pm25 is not None
        self.data = GoveeData(
            is_on=self.data.is_on,
            pm25=pm25,
            filter_life=self.data.filter_life,
            fan_mode=self.data.fan_mode,
        )
        self._notify()

    def _notify(self) -> None:
        for listener in list(self.listeners):
            listener()


class ControlledSleep:
    def __init__(self) -> None:
        self.waiters: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, delay: float) -> None:
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((delay, future))
        await future

    def release(self, delay: float) -> None:
        for waiter_delay, future in list(self.waiters):
            if waiter_delay == delay and not future.done():
                future.set_result(None)


def custom_auto_config(**overrides: int) -> CustomAutoConfig:
    return CustomAutoConfig.from_options(
        {**CUSTOM_AUTO_DEFAULTS, **overrides}
    )


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_immediate_rises_jump_directly_to_highest_required_speed() -> None:
    coordinator = FakeCoordinator(pm25=0)
    del coordinator.last_update_success
    controller = CustomAutoController(None, coordinator, custom_auto_config())

    await controller.async_activate()
    coordinator.set_pm25(10)
    await settle()
    coordinator.set_pm25(16)
    await settle()

    assert coordinator.commands == ["High", "Turbo"]
    assert controller.current_speed == 100
    await controller.async_stop()


@pytest.mark.asyncio
async def test_activation_from_off_deliberately_powers_on_before_off_detection() -> None:
    coordinator = FakeCoordinator(pm25=10, mode="Sleep", is_on=False)
    controller = CustomAutoController(None, coordinator, custom_auto_config())

    await controller.async_activate()

    assert controller.active is True
    assert coordinator.commands == ["High"]
    assert coordinator.data.is_on is True
    await controller.async_stop()


@pytest.mark.asyncio
async def test_activation_ignores_stale_pm_until_update_recovers() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Low")
    coordinator.last_update_success = False
    coordinator.last_pm25_update_success = False
    controller = CustomAutoController(None, coordinator, custom_auto_config())

    await controller.async_activate()

    assert coordinator.commands == []
    assert controller.current_speed == 40

    coordinator.set_update_success(True)
    await settle()

    assert coordinator.commands == ["Turbo"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_rapid_pm_updates_are_serialized_and_use_latest_highest_requirement() -> None:
    coordinator = FakeCoordinator(pm25=0)
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()

    coordinator.set_pm25(10)
    coordinator.set_pm25(16)
    await settle()

    assert coordinator.commands == ["Turbo"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_each_configured_downshift_delay_starts_independently() -> None:
    coordinator = FakeCoordinator(pm25=0)
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None,
        coordinator,
        custom_auto_config(
            custom_auto_delay_20=1,
            custom_auto_delay_40=2,
            custom_auto_delay_60=3,
            custom_auto_delay_80=4,
        ),
        sleep=sleep,
    )

    await controller.async_activate()
    await settle()

    assert sorted(delay for delay, _ in sleep.waiters) == [60, 120, 180, 240]
    await controller.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pm25", "delay", "expected_mode"),
    [(13, 300, "High"), (8, 300, "Medium"), (4, 300, "Low"), (2, 420, "Sleep")],
)
async def test_each_delayed_downshift_chooses_lowest_mature_speed(
    pm25: int, delay: int, expected_mode: str
) -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()

    coordinator.set_pm25(pm25)
    await settle()
    sleep.release(delay)
    await settle()

    assert coordinator.commands[-1] == expected_mode
    await controller.async_stop()


@pytest.mark.asyncio
async def test_downshift_threshold_is_inclusive() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()

    coordinator.set_pm25(13)
    await settle()
    coordinator.set_pm25(14)
    await settle()

    assert coordinator.commands == []
    sleep.release(300)
    await settle()
    assert coordinator.commands == ["High"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_default_return_threshold_three_permits_sleep_after_delay() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()

    coordinator.set_pm25(3)
    await settle()
    sleep.release(420)
    await settle()

    assert coordinator.commands == ["Sleep"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_external_off_deactivates_notifies_and_cancels_pending_timer() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    active_states: list[bool] = []
    controller.async_add_listener(lambda: active_states.append(controller.active))
    await controller.async_activate()
    coordinator.set_pm25(13)
    await settle()
    pending_timer = sleep.waiters[-1][1]

    coordinator.set_is_on(False)
    await settle()

    assert controller.active is False
    assert coordinator.listeners == []
    assert pending_timer.cancelled()
    assert active_states[-1] is False
    coordinator.set_pm25(20)
    sleep.release(300)
    await settle()
    assert coordinator.commands == []


@pytest.mark.asyncio
async def test_failed_coordinator_update_preserves_and_matures_timer() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(13)
    await settle()
    failed_timer = sleep.waiters[-1][1]

    coordinator.set_update_success(False)
    sleep.release(300)
    await settle()

    assert failed_timer.done()
    assert controller.pending_downshifts == ()
    assert controller.diagnostics()["mature_downshifts"] == [80]
    assert coordinator.commands == []

    coordinator.set_update_success(True)
    await settle()
    assert coordinator.commands == ["High"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_invalid_pm_sample_preserves_and_matures_timer() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(13)
    await settle()
    stale_timer = sleep.waiters[-1][1]

    coordinator.set_pm25(None)
    sleep.release(300)
    await settle()

    assert stale_timer.done()
    assert controller.pending_downshifts == ()
    assert controller.diagnostics()["mature_downshifts"] == [80]
    assert coordinator.commands == []

    coordinator.set_pm25(13)
    await settle()

    assert coordinator.commands == ["High"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_mature_timer_is_cleared_if_recovery_sample_no_longer_qualifies() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(13)
    await settle()

    coordinator.set_pm25(None)
    sleep.release(300)
    await settle()
    coordinator.set_pm25(15)
    await settle()

    assert coordinator.commands == []
    assert controller.diagnostics()["mature_downshifts"] == []
    await controller.async_stop()


@pytest.mark.asyncio
async def test_reselecting_auto_keeps_speed_and_existing_downshift_timers() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(2)
    await settle()
    waiters = list(sleep.waiters)

    await controller.async_activate()

    assert controller.current_speed == 100
    assert coordinator.commands == []
    assert sleep.waiters == waiters
    sleep.release(300)
    await settle()
    assert coordinator.commands == ["Low"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_reselecting_auto_still_applies_immediate_upward_correction() -> None:
    coordinator = FakeCoordinator(pm25=10, mode="High")
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()
    coordinator.data = GoveeData(
        is_on=True, pm25=16, filter_life=90, fan_mode="High"
    )

    await controller.async_activate()

    assert coordinator.commands == ["Turbo"]
    assert controller.current_speed == 100
    await controller.async_stop()


@pytest.mark.asyncio
async def test_activation_command_failure_rolls_back_all_ownership() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Sleep")
    coordinator.command_errors.append(RuntimeError("BLE unavailable"))
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    active_states: list[bool] = []
    controller.async_add_listener(lambda: active_states.append(controller.active))

    with pytest.raises(RuntimeError, match="BLE unavailable"):
        await controller.async_activate()

    assert controller.active is False
    assert controller.current_speed is None
    assert controller.pending_downshifts == ()
    assert coordinator.listeners == []
    assert active_states == [False]


@pytest.mark.asyncio
async def test_activation_cancellation_rolls_back_all_ownership() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Sleep")
    command_started = asyncio.Event()

    async def blocking_set_fan_mode(mode: str) -> None:
        coordinator.command_attempts.append(mode)
        command_started.set()
        await asyncio.Event().wait()

    coordinator.async_set_fan_mode = blocking_set_fan_mode  # type: ignore[method-assign]
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    activation_task = asyncio.create_task(controller.async_activate())
    await command_started.wait()

    activation_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await activation_task

    assert controller.active is False
    assert controller.current_speed is None
    assert controller.pending_downshifts == ()
    assert coordinator.listeners == []


@pytest.mark.asyncio
async def test_background_command_failure_is_logged_and_waits_for_update(
    caplog: pytest.LogCaptureFixture,
) -> None:
    coordinator = FakeCoordinator(pm25=0, mode="Sleep")
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()
    coordinator.command_errors.append(RuntimeError("BLE background failure"))

    coordinator.set_pm25(16)
    await settle()

    assert controller.active is True
    assert controller.current_speed == 20
    assert coordinator.command_attempts == ["Turbo"]
    assert "Custom auto evaluation failed" in caplog.text
    await settle()
    assert coordinator.command_attempts == ["Turbo"]

    coordinator.set_update_success(True)
    await settle()
    assert coordinator.command_attempts == ["Turbo", "Turbo"]
    assert coordinator.commands == ["Turbo"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_timer_expiry_does_not_bypass_command_failure_retry_gate() -> None:
    coordinator = FakeCoordinator(pm25=0, mode="Sleep")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    await settle()
    coordinator.command_errors.append(RuntimeError("BLE background failure"))

    coordinator.set_pm25(16)
    await settle()
    sleep.release(300)
    await settle()

    assert coordinator.command_attempts == ["Turbo"]
    assert controller.pending_downshifts == ()
    assert controller.diagnostics()["mature_downshifts"] == []

    coordinator.set_update_success(True)
    await settle()

    assert coordinator.command_attempts == ["Turbo", "Turbo"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_failed_mature_downshift_retries_after_successful_update_only() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(3)
    await settle()
    coordinator.command_errors.append(RuntimeError("BLE downshift failure"))

    sleep.release(420)
    await settle()

    assert coordinator.command_attempts == ["Sleep"]
    assert controller.diagnostics()["mature_downshifts"] == [20]
    await settle()
    assert coordinator.command_attempts == ["Sleep"]

    coordinator.set_update_success(True)
    await settle()

    assert coordinator.command_attempts == ["Sleep", "Sleep"]
    assert coordinator.commands == ["Sleep"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_failed_handoff_preserves_controller_and_downshift_state() -> None:
    coordinator = FakeCoordinator(pm25=16, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate()
    coordinator.set_pm25(3)
    await settle()
    sleep.release(300)
    await settle()
    pending_timer = next(
        future for delay, future in sleep.waiters if delay == 420
    )
    assert controller.current_speed == 40
    assert controller.diagnostics()["mature_downshifts"] == [40, 60, 80]

    async def failed_command() -> None:
        raise RuntimeError("handoff failed")

    with pytest.raises(RuntimeError, match="handoff failed"):
        await controller.async_handoff(failed_command)

    assert controller.active is True
    assert controller.current_speed == 40
    assert controller.pending_downshifts == (20,)
    assert controller.diagnostics()["mature_downshifts"] == [40, 60, 80]
    assert pending_timer.cancelled() is False
    assert coordinator.commands == ["Low", "Low"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_handoff_serializes_concurrent_reactivation() -> None:
    coordinator = FakeCoordinator(pm25=10, mode="High")
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()
    command_started = asyncio.Event()
    release_command = asyncio.Event()

    async def hardware_auto_command() -> None:
        command_started.set()
        await release_command.wait()
        await coordinator.async_set_fan_mode("Auto")

    handoff_task = asyncio.create_task(controller.async_handoff(hardware_auto_command))
    await command_started.wait()
    activation_task = asyncio.create_task(controller.async_activate())
    await settle()
    assert activation_task.done() is False

    release_command.set()
    await asyncio.gather(handoff_task, activation_task)

    assert controller.active is True
    assert coordinator.data.fan_mode == "High"
    assert coordinator.commands == ["Auto", "High"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_handoff_cancellation_reasserts_custom_auto_speed() -> None:
    coordinator = FakeCoordinator(pm25=10, mode="High")
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()
    hardware_command_applied = asyncio.Event()

    async def hardware_auto_command() -> None:
        await coordinator.async_set_fan_mode("Auto")
        hardware_command_applied.set()
        await asyncio.Event().wait()

    handoff_task = asyncio.create_task(controller.async_handoff(hardware_auto_command))
    await hardware_command_applied.wait()
    handoff_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await handoff_task

    assert controller.active is True
    assert controller.current_speed == 80
    assert coordinator.data.fan_mode == "High"
    assert coordinator.commands == ["Auto", "High"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_missing_pm_is_ignored_after_activation_and_commands_are_deduplicated() -> None:
    coordinator = FakeCoordinator(pm25=6, mode="Medium")
    controller = CustomAutoController(None, coordinator, custom_auto_config())
    await controller.async_activate()

    coordinator.set_pm25(None)
    coordinator.set_pm25(None)
    await settle()
    coordinator.set_pm25(6)
    coordinator.set_pm25(6)
    await settle()

    assert coordinator.commands == []
    await controller.async_stop()


@pytest.mark.asyncio
async def test_activation_with_unknown_pm_powers_on_at_existing_or_sleep_speed() -> None:
    coordinator = FakeCoordinator(pm25=None, mode="Auto")
    controller = CustomAutoController(None, coordinator, custom_auto_config())

    await controller.async_activate()

    assert coordinator.commands == ["Sleep"]
    await controller.async_stop()


@pytest.mark.asyncio
async def test_restore_retains_speed_applies_only_upward_correction_and_restarts_timers() -> None:
    coordinator = FakeCoordinator(pm25=10, mode="Low")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )

    await controller.async_activate(restored_speed=40, restoring=True)

    assert coordinator.commands == ["High"]
    assert controller.current_speed == 80
    assert controller.pending_downshifts == (80,)
    await controller.async_stop()


@pytest.mark.asyncio
async def test_cleanup_removes_listener_and_cancels_timers() -> None:
    coordinator = FakeCoordinator(pm25=13, mode="Turbo")
    sleep = ControlledSleep()
    controller = CustomAutoController(
        None, coordinator, custom_auto_config(), sleep=sleep
    )
    await controller.async_activate(restored_speed=100, restoring=True)
    await settle()
    waiters = [future for _, future in sleep.waiters]

    await controller.async_stop()
    await settle()

    assert coordinator.listeners == []
    assert controller.pending_downshifts == ()
    assert all(future.cancelled() for future in waiters)
