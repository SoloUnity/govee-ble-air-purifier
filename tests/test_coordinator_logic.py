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


@pytest.mark.asyncio
async def test_coordinator_fetches_power_status_pm25_and_filter_life() -> None:
    from custom_components.govee_ble_air_purifier.coordinator import GoveeCoordinator

    coordinator = GoveeCoordinator(None, FakeClient(), update_method_only=True)
    data = await coordinator._async_update_data()

    assert data == GoveeData(is_on=False, pm25=12, filter_life=87, fan_mode=None)
    assert POLLING_INTERVAL == timedelta(seconds=15)


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
