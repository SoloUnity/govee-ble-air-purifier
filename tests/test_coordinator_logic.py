import asyncio
from datetime import timedelta

import pytest

from custom_components.govee_ble_air_purifier.models import (
    NightLightState,
    PurifierState,
)
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE, get_profile

FAN_MODE_COMMANDS = H7124_PROFILE.fan_mode_commands


class FakeClient:
    def __init__(self) -> None:
        self.commands: list[object] = []
        self.power = False
        self.pm25 = 12
        self.filter_life = 87
        self.state_fetches = 0
        self.closed = False
        self.night_light: NightLightState | None = None
        self.fail_night_light_rgb = False

    async def async_get_state(self) -> PurifierState:
        self.state_fetches += 1
        return PurifierState(
            is_on=self.power,
            pm25=self.pm25,
            filter_life=self.filter_life,
            fan_mode=None,
            night_light=self.night_light,
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
        return PurifierState(is_on=True, fan_mode=mode)

    async def async_set_night_light_power(self, is_on: bool) -> NightLightState:
        current = self.night_light or NightLightState(brightness_percent=50)
        self.night_light = NightLightState(
            is_on=is_on,
            brightness_percent=current.brightness_percent,
            rgb_color=current.rgb_color,
        )
        self.commands.append(b"light_on" if is_on else b"light_off")
        return self.night_light

    async def async_set_night_light_brightness(
        self, brightness_percent: int
    ) -> NightLightState:
        current = self.night_light or NightLightState()
        self.night_light = NightLightState(
            is_on=True,
            brightness_percent=brightness_percent,
            rgb_color=current.rgb_color,
        )
        self.commands.append(("light_brightness", brightness_percent))
        return self.night_light

    async def async_set_night_light_rgb(
        self, rgb_color: tuple[int, int, int]
    ) -> NightLightState:
        if self.fail_night_light_rgb:
            raise RuntimeError("RGB write failed")
        current = self.night_light or NightLightState()
        self.night_light = NightLightState(
            is_on=current.is_on,
            brightness_percent=current.brightness_percent,
            rgb_color=rgb_color,
        )
        self.commands.append(("light_rgb", rgb_color))
        return NightLightState(rgb_color=rgb_color)

    async def async_close(self) -> None:
        self.closed = True


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


@pytest.mark.asyncio
async def test_coordinator_fetches_power_status_pm25_and_filter_life() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(FakeHass(), FakeClient())
    data = await coordinator._async_update_data()

    assert data == PurifierState(is_on=False, pm25=12, filter_life=87, fan_mode=None)
    assert coordinator.last_pm25_update_success is True
    assert coordinator.pm25_sample_revision == 1
    assert coordinator.poll_revision == 1
    assert coordinator.polling_interval == timedelta(seconds=10)

    await coordinator._async_update_data()

    assert coordinator.pm25_sample_revision == 2
    assert coordinator.poll_revision == 2


@pytest.mark.asyncio
async def test_coordinator_reuses_previous_pm25_when_latest_is_invalid() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.pm25 = None
    coordinator = GoveeCoordinator(FakeHass(), client)
    coordinator.data = PurifierState(
        is_on=True, pm25=42, filter_life=87, fan_mode="Low"
    )

    data = await coordinator._async_update_data()

    assert data == PurifierState(is_on=False, pm25=42, filter_life=87, fan_mode=None)
    assert coordinator.last_pm25_update_success is False
    assert coordinator.pm25_sample_revision == 0
    assert coordinator.poll_revision == 1


@pytest.mark.asyncio
async def test_coordinator_leaves_pm25_unknown_without_previous_valid_value() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.pm25 = None
    coordinator = GoveeCoordinator(FakeHass(), client)

    data = await coordinator._async_update_data()

    assert data == PurifierState(
        is_on=False, pm25=None, filter_life=87, fan_mode=None
    )


def test_coordinator_accepts_custom_polling_interval() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(
        FakeHass(),
        FakeClient(),
        polling_interval=timedelta(seconds=120),
    )

    assert coordinator.polling_interval == timedelta(seconds=120)


def test_coordinator_uses_profile_polling_interval() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(
        FakeHass(), FakeClient(), profile=get_profile("h7129")
    )

    assert coordinator.polling_interval == timedelta(seconds=3)


@pytest.mark.asyncio
async def test_setting_power_updates_data_without_full_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)
    coordinator.data = PurifierState(
        is_on=False,
        pm25=12,
        filter_life=87,
        fan_mode="Low",
        night_light=NightLightState(is_on=True, brightness_percent=50),
    )

    await coordinator.async_set_power(True)

    assert client.commands == [b"power_on"]
    assert client.state_fetches == 0
    assert coordinator.data == PurifierState(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Low",
        night_light=NightLightState(is_on=True, brightness_percent=50),
    )


@pytest.mark.asyncio
async def test_setting_fan_mode_updates_data_without_full_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.power = True
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)
    coordinator.data = PurifierState(
        is_on=True, pm25=12, filter_life=87, fan_mode="Low"
    )

    await coordinator.async_set_fan_mode("Turbo")

    assert client.commands == [FAN_MODE_COMMANDS["Turbo"]]
    assert client.state_fetches == 0
    assert coordinator.pm25_sample_revision == 0
    assert coordinator.poll_revision == 0
    assert coordinator.data == PurifierState(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Turbo",
    )


@pytest.mark.asyncio
async def test_disabled_telemetry_preserves_confirmed_light_state() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(FakeHass(), client)
    coordinator.data = PurifierState(
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 255, 0),
        )
    )

    data = await coordinator._async_update_data()

    assert data.night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(255, 255, 0),
    )


@pytest.mark.asyncio
async def test_night_light_settings_power_on_first_when_known_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)
    coordinator.data = PurifierState(
        is_on=True,
        night_light=NightLightState(
            is_on=False,
            brightness_percent=50,
            rgb_color=(255, 0, 0),
        ),
    )

    await coordinator.async_set_night_light(
        is_on=True,
        brightness_percent=100,
        rgb_color=(0, 0, 255),
    )

    assert client.commands == [
        b"light_on",
        ("light_brightness", 100),
        ("light_rgb", (0, 0, 255)),
    ]
    assert coordinator.data.night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(0, 0, 255),
    )


@pytest.mark.asyncio
async def test_night_light_setting_skips_power_when_known_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.night_light = NightLightState(is_on=True, brightness_percent=50)
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)
    coordinator.data = PurifierState(night_light=client.night_light)

    await coordinator.async_set_night_light(is_on=True, brightness_percent=1)

    assert client.commands == [("light_brightness", 1)]
    assert coordinator.data.night_light == NightLightState(
        is_on=True, brightness_percent=1
    )


@pytest.mark.asyncio
async def test_night_light_partial_failure_keeps_confirmed_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    client.fail_night_light_rgb = True
    coordinator = GoveeCoordinator(FakeHass(), client)
    scheduled: list[bool] = []
    monkeypatch.setattr(
        coordinator, "_schedule_background_refresh", lambda: scheduled.append(True)
    )
    coordinator.data = PurifierState(
        night_light=NightLightState(is_on=False, brightness_percent=50)
    )

    with pytest.raises(RuntimeError, match="RGB write failed"):
        await coordinator.async_set_night_light(
            is_on=True,
            brightness_percent=100,
            rgb_color=(0, 255, 0),
        )

    assert coordinator.data.night_light == NightLightState(
        is_on=True, brightness_percent=100
    )
    assert scheduled == [True]


@pytest.mark.asyncio
async def test_night_light_command_rejects_profile_without_capability() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(
        FakeHass(), FakeClient(), profile=get_profile("h7126")
    )

    with pytest.raises(ValueError, match="no night-light capability"):
        await coordinator.async_set_night_light(is_on=True)


@pytest.mark.asyncio
async def test_setting_fan_mode_turns_device_on_when_off_and_remembers_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)

    await coordinator.async_set_fan_mode("Turbo")

    assert client.commands == [b"power_on_and_" + FAN_MODE_COMMANDS["Turbo"]]
    assert coordinator.data == PurifierState(
        is_on=True,
        pm25=12,
        filter_life=87,
        fan_mode="Turbo",
    )


@pytest.mark.asyncio
async def test_concurrent_power_off_and_fan_mode_use_atomic_coordinator_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = RacingClient()
    coordinator = GoveeCoordinator(FakeHass(), client)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)
    coordinator.data = PurifierState(
        is_on=True, pm25=12, filter_life=87, fan_mode="Low"
    )

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
    assert coordinator.data == PurifierState(
        is_on=True, pm25=12, filter_life=87, fan_mode="Turbo"
    )


@pytest.mark.asyncio
async def test_coordinator_uses_profile_fan_modes_and_batches_power_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    client = FakeClient()
    coordinator = GoveeCoordinator(FakeHass(), client, profile=H7124_PROFILE)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)

    await coordinator.async_set_fan_mode("Auto")

    assert client.commands == [b"power_on_and_" + H7124_PROFILE.fan_mode_commands["Auto"]]
    with pytest.raises(ValueError, match="Unsupported fan mode"):
        await coordinator.async_set_fan_mode("Off")


@pytest.mark.asyncio
async def test_background_refresh_scheduling_is_coalesced() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    coordinator = GoveeCoordinator(hass, client)

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
        await coordinator.async_shutdown()
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_power_command_cancels_pending_background_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    coordinator = GoveeCoordinator(hass, client)
    coordinator.data = PurifierState(
        is_on=False, pm25=12, filter_life=87, fan_mode="Low"
    )

    try:
        coordinator._schedule_background_refresh()
        pending_refresh = hass.tasks[-1]

        await coordinator.async_set_power(True)
        await asyncio.sleep(0)

        assert pending_refresh.cancelled()
        assert client.commands == [b"power_on"]
    finally:
        await coordinator.async_shutdown()
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_fan_mode_command_cancels_pending_background_refresh() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    client.power = True
    coordinator = GoveeCoordinator(hass, client)
    coordinator.data = PurifierState(
        is_on=True, pm25=12, filter_life=87, fan_mode="Low"
    )

    try:
        coordinator._schedule_background_refresh()
        pending_refresh = hass.tasks[-1]

        await coordinator.async_set_fan_mode("Turbo")
        await asyncio.sleep(0)

        assert pending_refresh.cancelled()
        assert client.commands == [FAN_MODE_COMMANDS["Turbo"]]
    finally:
        await coordinator.async_shutdown()
        await _cleanup_tasks(hass.tasks)


@pytest.mark.asyncio
async def test_coordinator_shutdown_cancels_pending_background_refresh_and_delegates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    hass = FakeHass()
    client = FakeClient()
    coordinator = GoveeCoordinator(hass, client)
    coordinator._schedule_background_refresh()
    pending_refresh = hass.tasks[-1]
    delegated: list[bool] = []

    async def async_shutdown(_self) -> None:
        delegated.append(pending_refresh.done())

    monkeypatch.setattr(DataUpdateCoordinator, "async_shutdown", async_shutdown)

    await coordinator.async_shutdown()

    assert pending_refresh.cancelled()
    assert coordinator._background_refresh_task is None
    assert delegated == [True]
    assert client.closed is True


@pytest.mark.asyncio
async def test_commands_publish_with_async_set_updated_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(FakeHass(), FakeClient())
    coordinator.data = PurifierState(
        is_on=False, pm25=12, filter_life=87, fan_mode=None
    )
    published: list[PurifierState] = []

    def async_set_updated_data(data: PurifierState) -> None:
        published.append(data)
        coordinator.data = data

    monkeypatch.setattr(coordinator, "async_set_updated_data", async_set_updated_data)
    monkeypatch.setattr(coordinator, "_schedule_background_refresh", lambda: None)

    await coordinator.async_set_power(True)

    assert published == [
        PurifierState(is_on=True, pm25=12, filter_life=87, fan_mode=None)
    ]
