from types import SimpleNamespace

import pytest

import custom_components.govee_ble_air_purifier as integration
from custom_components.govee_ble_air_purifier import async_unload_entry


def test_connection_arbiter_is_shared_by_config_entries() -> None:
    hass = SimpleNamespace(data={})

    assert integration._connection_arbiter(hass) is integration._connection_arbiter(
        hass
    )


@pytest.mark.asyncio
async def test_successful_unload_stops_controller_and_coordinator() -> None:
    calls: list[str] = []

    async def stop_controller() -> None:
        calls.append("controller")

    async def stop_auto_resume() -> None:
        calls.append("auto_resume")

    async def stop_coordinator() -> None:
        calls.append("coordinator")

    async def unload_platforms(entry, platforms) -> bool:
        return True

    entry = SimpleNamespace(
        runtime_data=SimpleNamespace(
            auto_resume=SimpleNamespace(async_stop=stop_auto_resume),
            controller=SimpleNamespace(async_stop=stop_controller),
            coordinator=SimpleNamespace(async_shutdown=stop_coordinator),
        )
    )
    hass = SimpleNamespace(
        config_entries=SimpleNamespace(async_unload_platforms=unload_platforms)
    )

    assert await async_unload_entry(hass, entry) is True
    assert calls == ["auto_resume", "controller", "coordinator"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail_first_refresh", "expected_calls"),
    [
        (True, ["refresh", "coordinator"]),
        (
            False,
            ["refresh", "restore", "auto_resume", "controller", "coordinator"],
        ),
    ],
)
async def test_setup_failure_stops_controller_and_coordinator(
    monkeypatch: pytest.MonkeyPatch,
    fail_first_refresh: bool,
    expected_calls: list[str],
) -> None:
    calls: list[str] = []

    class FakeClient:
        def __init__(
            self,
            hass,
            address,
            *,
            profile,
            polling_interval_seconds,
            connection_arbiter,
        ) -> None:
            return None

    class FakeCoordinator:
        def __init__(self, hass, client, *, profile, polling_interval) -> None:
            return None

        async def async_config_entry_first_refresh(self) -> None:
            calls.append("refresh")
            if fail_first_refresh:
                raise RuntimeError("setup failed")

        async def async_shutdown(self) -> None:
            calls.append("coordinator")

    class FakeController:
        def __init__(self, hass, coordinator, config, *, config_entry) -> None:
            return None

        async def async_stop(self) -> None:
            calls.append("controller")

    class FakeAutoResume:
        def __init__(
            self, hass, coordinator, controller, *, config_entry
        ) -> None:
            return None

        async def async_stop(self) -> None:
            calls.append("auto_resume")

        async def async_restore_from_hass(self, unique_id: str) -> None:
            calls.append("restore")

    class FakeEntry:
        unique_id = "aabbccddeeff"
        data = {
            "address": "AA:BB:CC:DD:EE:FF",
            "name": "Purifier",
            "profile": "h7124",
        }
        options = {}
        runtime_data = None

        def add_update_listener(self, listener):
            return lambda: None

        def async_on_unload(self, callback) -> None:
            return None

    async def forward_entry_setups(entry, platforms) -> None:
        raise RuntimeError("setup failed")

    monkeypatch.setattr(integration, "GoveeBleClient", FakeClient)
    monkeypatch.setattr(integration, "GoveeCoordinator", FakeCoordinator)
    monkeypatch.setattr(integration, "CustomAutoController", FakeController)
    monkeypatch.setattr(integration, "AutoResumeManager", FakeAutoResume)
    entry = FakeEntry()
    hass = SimpleNamespace(
        data={},
        config_entries=SimpleNamespace(
            async_forward_entry_setups=forward_entry_setups
        )
    )

    with pytest.raises(RuntimeError, match="setup failed"):
        await integration.async_setup_entry(hass, entry)

    assert calls == expected_calls
    assert entry.runtime_data is None
