import asyncio
from datetime import timedelta

import pytest

from custom_components.govee_ble_air_purifier.coordinator import POLLING_INTERVAL, GoveeData
from custom_components.govee_ble_air_purifier.protocol import FAN_MODE_COMMANDS
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[bytes] = []
        self.power = False
        self.pm25 = 12
        self.filter_life = 87
        self.state_fetches = 0

    async def async_get_state(self) -> GoveeData:
        self.state_fetches += 1
        return GoveeData(
            is_on=self.power,
            pm25=self.pm25,
            filter_life=self.filter_life,
            fan_mode=None,
        )

    async def async_set_power(self, is_on: bool) -> None:
        self.power = is_on
        self.commands.append(b"power_on" if is_on else b"power_off")
        return self.power

    async def async_set_fan_mode(self, mode: str) -> None:
        self.commands.append(FAN_MODE_COMMANDS[mode])
        return mode

    async def async_set_power_and_fan_mode(self, mode: str) -> None:
        self.power = True
        self.commands.append(b"power_on_and_" + FAN_MODE_COMMANDS[mode])
        return GoveeData(is_on=True, fan_mode=mode)


class FakeHass:
    def __init__(self) -> None:
        self.tasks: list[asyncio.Task] = []

    def async_create_task(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.append(task)
        return task


class RacingClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.power = True
        self.power_off_started = asyncio.Event()
        self.release_power_off = asyncio.Event()

    async def async_set_power(self, is_on: bool) -> bool:
        if not is_on:
            self.power_off_started.set()
            await self.release_power_off.wait()
        return await super().async_set_power(is_on)


async def _cleanup_tasks(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _enable_background_refresh_tasks(coordinator, hass: FakeHass) -> None:
    coordinator._standalone = False
    coordinator._hass = hass


@pytest.mark.asyncio
async def test_coordinator_fetches_power_status_pm25_and_filter_life() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(None, FakeClient(), update_method_only=True)
    data = await coordinator._async_update_data()

    assert data == GoveeData(is_on=False, pm25=12, filter_life=87, fan_mode=None)
    assert coordinator.last_pm25_update_success is True
    assert POLLING_INTERVAL == timedelta(seconds=15)


@pytest.mark.asyncio
async def test_coordinator_reuses_previous_pm25_when_latest_is_invalid() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.pm25 = None
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=True, pm25=42, filter_life=87, fan_mode="Low")

    data = await coordinator._async_update_data()

    assert data == GoveeData(is_on=False, pm25=42, filter_life=87, fan_mode=None)
    assert coordinator.last_pm25_update_success is False


@pytest.mark.asyncio
async def test_coordinator_leaves_pm25_unknown_without_previous_valid_value() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.pm25 = None
    coordinator = GoveeCoordinator(None, client, update_method_only=True)

    data = await coordinator._async_update_data()

    assert data == GoveeData(is_on=False, pm25=None, filter_life=87, fan_mode=None)


def test_coordinator_accepts_custom_polling_interval() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(
        None,
        FakeClient(),
        polling_interval=timedelta(seconds=120),
        update_method_only=True,
    )

    assert coordinator.polling_interval == timedelta(seconds=120)


@pytest.mark.asyncio
async def test_setting_power_updates_data_without_full_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=False, pm25=12, filter_life=87, fan_mode="Low")

    await coordinator.async_set_power(True)

    assert client.commands == [b"power_on"]
    assert client.state_fetches == 0
    assert coordinator.data == GoveeData(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Low",
    )


@pytest.mark.asyncio
async def test_setting_fan_mode_updates_data_without_full_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.power = True
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=True, pm25=12, filter_life=87, fan_mode="Low")

    await coordinator.async_set_fan_mode("Turbo")

    assert client.commands == [FAN_MODE_COMMANDS["Turbo"]]
    assert client.state_fetches == 0
    assert coordinator.data == GoveeData(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Turbo",
    )


@pytest.mark.asyncio
async def test_setting_fan_mode_turns_device_on_when_off_and_remembers_mode() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(None, client, update_method_only=True)

    await coordinator.async_set_fan_mode("Turbo")

    assert client.commands == [b"power_on_and_" + FAN_MODE_COMMANDS["Turbo"]]
    assert coordinator.data == GoveeData(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Turbo",
    )


@pytest.mark.asyncio
async def test_concurrent_power_off_and_fan_mode_use_atomic_coordinator_state() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = RacingClient()
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=True, pm25=12, filter_life=87, fan_mode="Low")

    power_task = asyncio.create_task(coordinator.async_set_power(False))
    await client.power_off_started.wait()
    mode_task = asyncio.create_task(coordinator.async_set_fan_mode("Turbo"))
    await asyncio.sleep(0)
    client.release_power_off.set()
    await asyncio.gather(power_task, mode_task)

    assert client.commands == [
        b"power_off",
        b"power_on_and_" + FAN_MODE_COMMANDS["Turbo"],
    ]
    assert coordinator.data == GoveeData(
        is_on=True, pm25=12, filter_life=87, fan_mode="Turbo"
    )


@pytest.mark.asyncio
async def test_coordinator_uses_profile_fan_modes_and_batches_power_on() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(
        None, client, profile=H7124_PROFILE, update_method_only=True
    )

    await coordinator.async_set_fan_mode("Auto")

    assert client.commands == [b"power_on_and_" + H7124_PROFILE.fan_mode_commands["Auto"]]
    with pytest.raises(ValueError, match="Unsupported fan mode"):
        await coordinator.async_set_fan_mode("Off")


@pytest.mark.asyncio
async def test_background_refresh_scheduling_is_coalesced() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    coordinator = GoveeCoordinator(None, FakeClient(), update_method_only=True)
    _enable_background_refresh_tasks(coordinator, hass)

    try:
        coordinator._schedule_background_refresh()
        first_task = hass.tasks[-1]

        coordinator._schedule_background_refresh()
        second_task = hass.tasks[-1]
        await asyncio.sleep(0)

        assert first_task.cancelled()
        assert second_task is not first_task
        assert not second_task.cancelled()
    finally:
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_power_command_cancels_pending_background_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=False, pm25=12, filter_life=87, fan_mode="Low")
    _enable_background_refresh_tasks(coordinator, hass)

    try:
        coordinator._schedule_background_refresh()
        pending_refresh = hass.tasks[-1]

        await coordinator.async_set_power(True)
        await asyncio.sleep(0)

        assert pending_refresh.cancelled()
        assert client.commands == [b"power_on"]
    finally:
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_fan_mode_command_cancels_pending_background_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    client.power = True
    coordinator = GoveeCoordinator(None, client, update_method_only=True)
    coordinator.data = GoveeData(is_on=True, pm25=12, filter_life=87, fan_mode="Low")
    _enable_background_refresh_tasks(coordinator, hass)

    try:
        coordinator._schedule_background_refresh()
        pending_refresh = hass.tasks[-1]

        await coordinator.async_set_fan_mode("Turbo")
        await asyncio.sleep(0)

        assert pending_refresh.cancelled()
        assert client.commands == [FAN_MODE_COMMANDS["Turbo"]]
    finally:
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_coordinator_shutdown_cancels_pending_background_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    coordinator = GoveeCoordinator(None, FakeClient(), update_method_only=True)
    _enable_background_refresh_tasks(coordinator, hass)
    coordinator._schedule_background_refresh()
    pending_refresh = hass.tasks[-1]

    await coordinator.async_shutdown()

    assert pending_refresh.cancelled()
    assert coordinator._background_refresh_task is None
