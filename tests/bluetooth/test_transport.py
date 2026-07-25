import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import GoveeBleClientError
from custom_components.govee_ble_air_purifier.bluetooth import transport
from tests.helpers.ha_stubs import install_modules


class FakeClient:
    def __init__(self, events: list[str], *, disconnect_error: Exception | None = None):
        self.events = events
        self.disconnect_error = disconnect_error
        self.is_connected = True

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        self.is_connected = False
        if self.disconnect_error is not None:
            raise self.disconnect_error


def _install_connection_modules(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    device: Any = SimpleNamespace(name="Purifier"),
    client: FakeClient | None = None,
    disconnected_callbacks: list[Any] | None = None,
) -> FakeClient:
    connected_client = client or FakeClient(events)

    def async_ble_device_from_address(*args: Any, **kwargs: Any) -> Any:
        events.append("lookup")
        return device

    async def close_stale_connections(_device: Any) -> None:
        events.append("close_stale")

    async def establish_connection(**kwargs: Any) -> FakeClient:
        events.append("establish")
        if disconnected_callbacks is not None:
            disconnected_callbacks.append(kwargs["disconnected_callback"])
        return connected_client

    install_modules(
        monkeypatch,
        {
            "bleak_retry_connector": {
                "BleakClientWithServiceCache": object,
                "close_stale_connections": close_stale_connections,
                "establish_connection": establish_connection,
            },
            "homeassistant.components.bluetooth": {
                "async_ble_device_from_address": async_ble_device_from_address,
            },
        },
    )
    return connected_client


@pytest.mark.asyncio
async def test_connection_stages_run_in_order_with_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    disconnected_callbacks: list[Any] = []
    client = _install_connection_modules(
        monkeypatch, events, disconnected_callbacks=disconnected_callbacks
    )
    deadlines: list[float] = []
    original_wait_until = transport._async_wait_until

    async def recording_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        return await original_wait_until(awaitable, deadline)

    monkeypatch.setattr(transport, "_async_wait_until", recording_wait_until)
    deadline = asyncio.get_running_loop().time() + 10.0

    assert await transport.async_establish_connection(
        object(),
        "AA:BB:CC:DD:EE:FF",
        lambda _client: None,
        deadline=deadline,
    ) is client

    assert events == ["lookup", "close_stale", "establish"]
    assert deadlines == [deadline, deadline]
    assert len(disconnected_callbacks) == 1


@pytest.mark.asyncio
async def test_unavailable_device_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events, device=None)

    with pytest.raises(GoveeBleClientError, match="BLE device .* is not available"):
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert events == ["lookup"]


@pytest.mark.asyncio
async def test_stage_timeout_is_translated_without_extending_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events)
    deadlines: list[float] = []

    async def timeout_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(transport, "_async_wait_until", timeout_wait_until)

    with pytest.raises(GoveeBleClientError, match="Timed out waiting"):
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=42.0,
        )

    assert deadlines == [42.0]
    assert events == ["lookup"]


@pytest.mark.asyncio
async def test_explicit_disconnect_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)
    deadlines: list[float] = []
    original_wait_until = transport._async_wait_until

    async def recording_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        return await original_wait_until(awaitable, deadline)

    monkeypatch.setattr(transport, "_async_wait_until", recording_wait_until)
    deadline = asyncio.get_running_loop().time() + 10.0

    await transport.async_disconnect(client, deadline=deadline)

    assert events == ["disconnect"]
    assert deadlines == [deadline]
    assert client.is_connected is False


@pytest.mark.asyncio
async def test_disconnect_error_is_suppressed() -> None:
    events: list[str] = []
    client = FakeClient(events, disconnect_error=RuntimeError("cleanup failed"))

    await transport.async_disconnect(
        client, deadline=asyncio.get_running_loop().time() + 10.0
    )

    assert events == ["disconnect"]


@pytest.mark.asyncio
async def test_disconnect_timeout_is_suppressed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events)

    async def timeout_wait_until(awaitable: Any, _deadline: float) -> Any:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(transport, "_async_wait_until", timeout_wait_until)

    await transport.async_disconnect(client, deadline=42.0)

    assert events == []
