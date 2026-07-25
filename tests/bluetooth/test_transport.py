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

    async def disconnect(self) -> None:
        self.events.append("disconnect")
        if self.disconnect_error is not None:
            raise self.disconnect_error


def _install_connection_modules(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    device: Any = SimpleNamespace(name="Purifier"),
    client: FakeClient | None = None,
) -> FakeClient:
    connected_client = client or FakeClient(events)

    def async_ble_device_from_address(*args: Any, **kwargs: Any) -> Any:
        events.append("lookup")
        return device

    async def close_stale_connections(_device: Any) -> None:
        events.append("close_stale")

    async def establish_connection(**_kwargs: Any) -> FakeClient:
        events.append("establish")
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
    client = _install_connection_modules(monkeypatch, events)
    deadlines: list[float] = []
    original_wait_until = transport._async_wait_until

    async def recording_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        return await original_wait_until(awaitable, deadline)

    async def operation(passed_client: FakeClient) -> str:
        assert passed_client is client
        events.append("operation")
        return "result"

    monkeypatch.setattr(transport, "_async_wait_until", recording_wait_until)
    deadline = asyncio.get_running_loop().time() + 10.0

    assert (
        await transport.async_with_connection(
            object(), "AA:BB:CC:DD:EE:FF", operation, deadline=deadline
        )
        == "result"
    )
    assert events == ["lookup", "close_stale", "establish", "operation", "disconnect"]
    assert deadlines == [deadline, deadline, deadline]


@pytest.mark.asyncio
async def test_unavailable_device_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events, device=None)

    with pytest.raises(GoveeBleClientError, match="BLE device .* is not available"):
        await transport.async_with_connection(
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
        await transport.async_with_connection(
            object(), "AA:BB:CC:DD:EE:FF", lambda _client: None, deadline=42.0
        )

    assert deadlines == [42.0]
    assert events == ["lookup"]


@pytest.mark.asyncio
async def test_disconnect_error_does_not_fail_successful_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events, disconnect_error=RuntimeError("cleanup failed"))
    _install_connection_modules(monkeypatch, events, client=client)

    async def operation(_client: FakeClient) -> str:
        events.append("operation")
        return "result"

    assert (
        await transport.async_with_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            operation,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )
        == "result"
    )
    assert events[-2:] == ["operation", "disconnect"]


@pytest.mark.asyncio
async def test_disconnect_error_does_not_mask_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    client = FakeClient(events, disconnect_error=RuntimeError("cleanup failed"))
    _install_connection_modules(monkeypatch, events, client=client)

    async def operation(_client: FakeClient) -> None:
        events.append("operation")
        raise ValueError("primary failed")

    with pytest.raises(ValueError, match="primary failed"):
        await transport.async_with_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            operation,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert events[-2:] == ["operation", "disconnect"]


@pytest.mark.asyncio
async def test_disconnect_timeout_does_not_mask_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events)
    original_wait_until = transport._async_wait_until
    wait_count = 0

    async def timeout_disconnect(awaitable: Any, deadline: float) -> Any:
        nonlocal wait_count
        wait_count += 1
        if wait_count == 3:
            awaitable.close()
            raise TimeoutError
        return await original_wait_until(awaitable, deadline)

    async def operation(_client: FakeClient) -> None:
        events.append("operation")
        raise ValueError("primary failed")

    monkeypatch.setattr(transport, "_async_wait_until", timeout_disconnect)

    with pytest.raises(ValueError, match="primary failed"):
        await transport.async_with_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            operation,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert wait_count == 3
    assert events == ["lookup", "close_stale", "establish", "operation"]
