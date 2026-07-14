from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier import async_unload_entry


@pytest.mark.asyncio
async def test_successful_unload_stops_controller_and_coordinator() -> None:
    calls: list[str] = []

    async def stop_controller() -> None:
        calls.append("controller")

    async def stop_coordinator() -> None:
        calls.append("coordinator")

    async def unload_platforms(entry, platforms) -> bool:
        return True

    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            controller=SimpleNamespace(async_stop=stop_controller),
            coordinator=SimpleNamespace(async_shutdown=stop_coordinator),
        )
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_unload_platforms=unload_platforms)
    )

    assert await async_unload_entry(hass, entry) is True
    assert set(calls) == {"controller", "coordinator"}
