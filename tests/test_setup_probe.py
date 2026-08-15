"""Tests for bounded, read-only setup validation."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import GoveeBleClientError
from custom_components.govee_ble_air_purifier.profiles import get_profile
from custom_components.govee_ble_air_purifier import setup_probe


class _ProbeClient:
    instances: list[_ProbeClient] = []
    state_error: BaseException | None = None
    state_waiter: asyncio.Event | None = None
    close_waiter: asyncio.Event | None = None

    def __init__(self, hass: object, address: str, **kwargs: object) -> None:
        self.hass = hass
        self.address = address
        self.kwargs = kwargs
        self.state_calls = 0
        self.close_calls = 0
        self.instances.append(self)

    async def async_get_state(self) -> object:
        self.state_calls += 1
        if self.state_waiter is not None:
            await self.state_waiter.wait()
        if self.state_error is not None:
            raise self.state_error
        return SimpleNamespace(is_on=True, pm25=4, filter_life=90)

    async def async_close(self) -> None:
        self.close_calls += 1
        if self.close_waiter is not None:
            await self.close_waiter.wait()


@pytest.fixture(autouse=True)
def probe_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _ProbeClient.instances = []
    _ProbeClient.state_error = None
    _ProbeClient.state_waiter = None
    _ProbeClient.close_waiter = None
    monkeypatch.setattr(setup_probe, "GoveeBleClient", _ProbeClient)


@pytest.mark.asyncio
async def test_probe_reads_state_and_always_closes_client() -> None:
    hass = object()
    profile = get_profile("h7124")

    await setup_probe.async_probe_device(hass, "AA:BB:CC:DD:EE:24", profile)

    client = _ProbeClient.instances[0]
    assert client.hass is hass
    assert client.address == "AA:BB:CC:DD:EE:24"
    assert client.kwargs == {
        "profile": profile,
        "polling_interval_seconds": 10,
    }
    assert client.state_calls == 1
    assert client.close_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "translation_key"),
    [
        (GoveeBleClientError("unreachable"), "cannot_connect"),
        (ValueError("invalid decoded status"), "invalid_response"),
    ],
)
async def test_probe_translates_failures_and_still_closes(
    error: BaseException, translation_key: str
) -> None:
    _ProbeClient.state_error = error

    with pytest.raises(setup_probe.SetupProbeError) as raised:
        await setup_probe.async_probe_device(
            object(), "AA:BB:CC:DD:EE:24", get_profile("h7124")
        )

    assert raised.value.translation_key == translation_key
    assert _ProbeClient.instances[0].close_calls == 1


@pytest.mark.asyncio
async def test_probe_timeout_is_bounded_and_closes() -> None:
    _ProbeClient.state_waiter = asyncio.Event()

    with pytest.raises(setup_probe.SetupProbeError) as raised:
        await setup_probe.async_probe_device(
            object(),
            "AA:BB:CC:DD:EE:24",
            get_profile("h7124"),
            timeout=0.001,
        )

    assert raised.value.translation_key == "probe_timeout"
    assert _ProbeClient.instances[0].close_calls == 1


@pytest.mark.asyncio
async def test_cleanup_timeout_is_reported_and_cleanup_remains_observed() -> None:
    close_waiter = asyncio.Event()
    _ProbeClient.close_waiter = close_waiter

    with pytest.raises(setup_probe.SetupProbeError) as raised:
        await setup_probe.async_probe_device(
            object(),
            "AA:BB:CC:DD:EE:24",
            get_profile("h7124"),
            cleanup_timeout=0.001,
        )

    assert raised.value.translation_key == "probe_cleanup_failed"
    assert _ProbeClient.instances[0].close_calls == 1
    assert setup_probe._BACKGROUND_CLEANUPS
    close_waiter.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert not setup_probe._BACKGROUND_CLEANUPS


@pytest.mark.asyncio
async def test_cancellation_closes_before_it_propagates() -> None:
    _ProbeClient.state_waiter = asyncio.Event()
    task = asyncio.create_task(
        setup_probe.async_probe_device(
            object(), "AA:BB:CC:DD:EE:24", get_profile("h7124")
        )
    )
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert _ProbeClient.instances[0].close_calls == 1
