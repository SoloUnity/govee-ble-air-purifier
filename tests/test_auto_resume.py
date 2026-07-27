import asyncio

import pytest

from custom_components.govee_ble_air_purifier.auto_resume import (
    AUTO_MODE_CUSTOM,
    AUTO_MODE_HARDWARE,
    AutoResumeManager,
    AutoResumeState,
)
from custom_components.govee_ble_air_purifier.models import PurifierState
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE


SPEED_TO_MODE = {
    20: "Sleep",
    40: "Low",
    60: "Medium",
    80: "High",
    100: "Turbo",
}


class FakeCoordinator:
    profile = H7124_PROFILE

    def __init__(self, *, is_on: bool = True, mode: str | None = "Low") -> None:
        self.data = PurifierState(is_on=is_on, pm25=7, filter_life=90, fan_mode=mode)
        self.poll_revision = 0
        self.commands: list[str] = []
        self.fail_power = False
        self.fail_modes: set[str] = set()
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def _publish(self, data: PurifierState) -> None:
        self.data = data
        for listener in list(self.listeners):
            listener()

    def poll(self, is_on: bool) -> None:
        self.poll_revision += 1
        self._publish(
            PurifierState(
                is_on=is_on,
                pm25=7,
                filter_life=90,
                fan_mode=self.data.fan_mode,
            )
        )

    async def async_set_power(self, is_on: bool) -> None:
        self.commands.append(f"power:{is_on}")
        if self.fail_power:
            raise RuntimeError("power failed")
        self._publish(
            PurifierState(
                is_on=is_on,
                pm25=7,
                filter_life=90,
                fan_mode=self.data.fan_mode if is_on else None,
            )
        )

    async def async_set_fan_mode(self, mode: str) -> None:
        self.commands.append(f"mode:{mode}")
        if mode in self.fail_modes:
            raise RuntimeError(f"{mode} failed")
        self._publish(
            PurifierState(is_on=True, pm25=7, filter_life=90, fan_mode=mode)
        )


class FakeController:
    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.coordinator = coordinator
        self.active = False
        self.current_speed = 60
        self.activations: list[tuple[int | None, bool, bool]] = []
        self.deactivations = 0
        self.listeners = []

    def async_add_listener(self, listener):
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def _notify(self) -> None:
        for listener in list(self.listeners):
            listener()

    async def async_activate(
        self,
        *,
        restored_speed: int | None = None,
        restoring: bool = False,
        force: bool = False,
    ) -> None:
        self.activations.append((restored_speed, restoring, force))
        if restored_speed is not None:
            self.current_speed = restored_speed
        if self.active and not force:
            return
        was_active = self.active
        self.active = True
        try:
            await self.coordinator.async_set_fan_mode(
                SPEED_TO_MODE[self.current_speed]
            )
        except BaseException:
            self.active = was_active
            raise
        self._notify()

    async def async_deactivate(self) -> None:
        if not self.active:
            return
        self.deactivations += 1
        self.active = False
        self._notify()

    async def async_handoff(self, command) -> None:
        await command()
        await self.async_deactivate()


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def make_manager(
    *, is_on: bool = True, mode: str | None = "Low"
) -> tuple[AutoResumeManager, FakeCoordinator, FakeController]:
    coordinator = FakeCoordinator(is_on=is_on, mode=mode)
    controller = FakeController(coordinator)
    manager = AutoResumeManager(None, coordinator, controller)
    return manager, coordinator, controller


@pytest.mark.asyncio
async def test_hardware_auto_is_suspended_and_resumed_by_ha_power() -> None:
    manager, coordinator, _controller = make_manager()

    await manager.async_set_hardware_auto()
    await manager.async_turn_off()

    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_HARDWARE, suspended=True
    )
    assert coordinator.data.is_on is False

    await manager.async_turn_on()

    assert manager.state == AutoResumeState(mode=AUTO_MODE_HARDWARE)
    assert coordinator.commands == ["mode:Auto", "power:False", "mode:Auto"]
    await manager.async_stop()


@pytest.mark.asyncio
async def test_custom_auto_is_suspended_and_resumed_at_remembered_speed() -> None:
    manager, coordinator, controller = make_manager()

    await manager.async_enable_custom_auto()
    await manager.async_turn_off()

    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_CUSTOM, suspended=True, custom_speed=60
    )
    assert controller.active is False

    await manager.async_turn_on()

    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_CUSTOM, custom_speed=60
    )
    assert controller.activations == [
        (None, False, False),
        (60, False, True),
    ]
    assert coordinator.commands == [
        "mode:Medium",
        "power:False",
        "mode:Medium",
    ]
    await manager.async_stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("auto_mode", [AUTO_MODE_HARDWARE, AUTO_MODE_CUSTOM])
async def test_physical_power_cycle_resumes_selected_auto_mode(
    auto_mode: str,
) -> None:
    manager, coordinator, controller = make_manager()
    if auto_mode == AUTO_MODE_HARDWARE:
        await manager.async_set_hardware_auto()
    else:
        await manager.async_enable_custom_auto()
    coordinator.commands.clear()

    coordinator.poll(False)
    await settle()

    assert manager.state.suspended is True
    assert controller.active is False

    coordinator.poll(True)
    await settle()

    assert manager.state.mode == auto_mode
    assert manager.state.suspended is False
    expected_mode = "Auto" if auto_mode == AUTO_MODE_HARDWARE else "Medium"
    assert coordinator.commands == [f"mode:{expected_mode}"]
    await manager.async_stop()


@pytest.mark.asyncio
async def test_suspended_restore_does_not_power_on_until_power_is_observed() -> None:
    manager, coordinator, _controller = make_manager(is_on=False, mode=None)

    await manager.async_restore(
        AUTO_MODE_HARDWARE, suspended=True, custom_speed=None
    )

    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_HARDWARE, suspended=True
    )
    assert coordinator.commands == []

    coordinator.poll(True)
    await settle()

    assert coordinator.commands == ["mode:Auto"]
    assert manager.state == AutoResumeState(mode=AUTO_MODE_HARDWARE)
    await manager.async_stop()


@pytest.mark.asyncio
async def test_command_publication_does_not_duplicate_resume() -> None:
    manager, coordinator, _controller = make_manager(is_on=False, mode=None)
    await manager.async_restore(
        AUTO_MODE_HARDWARE, suspended=True, custom_speed=None
    )

    await manager.async_turn_on()
    await settle()

    assert coordinator.commands == ["mode:Auto"]
    await manager.async_stop()


@pytest.mark.asyncio
async def test_failed_resume_stays_suspended_and_retries_on_next_poll() -> None:
    manager, coordinator, _controller = make_manager(is_on=False, mode=None)
    await manager.async_restore(
        AUTO_MODE_HARDWARE, suspended=True, custom_speed=None
    )
    coordinator.fail_modes.add("Auto")

    with pytest.raises(RuntimeError, match="Auto failed"):
        await manager.async_turn_on()

    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_HARDWARE, suspended=True
    )

    coordinator.fail_modes.clear()
    coordinator.poll(True)
    await settle()

    assert coordinator.commands == ["mode:Auto", "mode:Auto"]
    assert manager.state == AutoResumeState(mode=AUTO_MODE_HARDWARE)
    await manager.async_stop()


@pytest.mark.asyncio
async def test_explicit_manual_mode_clears_auto_resume_intent() -> None:
    manager, coordinator, _controller = make_manager()
    await manager.async_set_hardware_auto()

    await manager.async_set_manual_mode("High")
    coordinator.poll(False)
    coordinator.poll(True)
    await settle()

    assert manager.state == AutoResumeState()
    assert coordinator.commands == ["mode:Auto", "mode:High"]
    await manager.async_stop()


@pytest.mark.asyncio
async def test_disabling_suspended_custom_auto_applies_hardware_auto_when_on() -> None:
    manager, coordinator, _controller = make_manager(is_on=False, mode=None)
    await manager.async_restore(
        AUTO_MODE_CUSTOM, suspended=True, custom_speed=60
    )
    coordinator.data = PurifierState(is_on=True, fan_mode="Medium")

    await manager.async_disable_custom_auto()

    assert manager.state == AutoResumeState(mode=AUTO_MODE_HARDWARE)
    assert coordinator.commands == ["mode:Auto"]
    await manager.async_stop()


@pytest.mark.asyncio
async def test_suspended_custom_restore_while_on_forces_remembered_speed() -> None:
    manager, coordinator, controller = make_manager(is_on=True, mode="Medium")

    await manager.async_restore(
        AUTO_MODE_CUSTOM, suspended=True, custom_speed=60
    )
    await settle()

    assert controller.activations == [(60, True, True)]
    assert coordinator.commands == ["mode:Medium"]
    assert manager.state == AutoResumeState(
        mode=AUTO_MODE_CUSTOM, custom_speed=60
    )
    await manager.async_stop()


@pytest.mark.asyncio
async def test_explicit_manual_override_wins_over_running_physical_resume() -> None:
    manager, coordinator, _controller = make_manager(is_on=False, mode=None)
    await manager.async_restore(
        AUTO_MODE_HARDWARE, suspended=True, custom_speed=None
    )
    command_started = asyncio.Event()
    release_command = asyncio.Event()
    original_set_fan_mode = coordinator.async_set_fan_mode

    async def blocking_set_fan_mode(mode: str) -> None:
        if mode == "Auto":
            command_started.set()
            await release_command.wait()
        await original_set_fan_mode(mode)

    coordinator.async_set_fan_mode = blocking_set_fan_mode  # type: ignore[method-assign]
    coordinator.poll(True)
    await command_started.wait()
    manual_task = asyncio.create_task(manager.async_set_manual_mode("High"))
    await asyncio.sleep(0)

    assert manual_task.done() is False

    release_command.set()
    await manual_task
    await settle()

    assert coordinator.commands == ["mode:Auto", "mode:High"]
    assert manager.state == AutoResumeState()
    await manager.async_stop()
