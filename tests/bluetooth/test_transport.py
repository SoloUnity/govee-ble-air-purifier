import asyncio
import logging
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


def test_device_log_id_is_stable_distinct_and_non_sensitive() -> None:
    first = transport.device_log_id("AA:BB:CC:DD:EE:01")
    second = transport.device_log_id("AA:BB:CC:DD:EE:02")

    assert first == transport.device_log_id("aa:bb:cc:dd:ee:01")
    assert first != second
    assert len(first) == len(second) == 8
    assert ":" not in first


def _install_connection_modules(
    monkeypatch: pytest.MonkeyPatch,
    events: list[str],
    *,
    device: Any = SimpleNamespace(name="Purifier"),
    client: FakeClient | None = None,
    disconnected_callbacks: list[Any] | None = None,
    establish_calls: list[dict[str, Any]] | None = None,
) -> FakeClient:
    connected_client = client or FakeClient(events)

    def async_ble_device_from_address(*args: Any, **kwargs: Any) -> Any:
        events.append("lookup")
        return device

    async def close_stale_connections(_device: Any) -> None:
        events.append("close_stale")

    async def establish_connection(**kwargs: Any) -> FakeClient:
        events.append("establish")
        if establish_calls is not None:
            establish_calls.append(kwargs)
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
    establish_calls: list[dict[str, Any]] = []
    client = _install_connection_modules(
        monkeypatch,
        events,
        disconnected_callbacks=disconnected_callbacks,
        establish_calls=establish_calls,
    )
    deadlines: list[float] = []
    original_wait_until = transport._async_wait_until

    async def recording_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        return await original_wait_until(awaitable, deadline)

    monkeypatch.setattr(transport, "_async_wait_until", recording_wait_until)
    deadline = asyncio.get_running_loop().time() + 10.0

    assert (
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=deadline,
        )
        is client
    )

    assert events == ["lookup", "close_stale", "establish"]
    assert deadlines == [deadline]
    assert len(disconnected_callbacks) == 1
    assert establish_calls[0]["max_attempts"] == transport.MAX_CONNECTION_ATTEMPTS


@pytest.mark.asyncio
async def test_unavailable_device_fails_before_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events, device=None)

    with pytest.raises(
        GoveeBleClientError, match="BLE device .* is not available"
    ) as exc_info:
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=asyncio.get_running_loop().time() + 10.0,
        )

    assert events == ["lookup"]
    assert "AA:BB:CC:DD:EE:FF" not in str(exc_info.value)
    assert transport.device_log_id("AA:BB:CC:DD:EE:FF") in str(exc_info.value)


@pytest.mark.asyncio
async def test_stage_timeout_is_translated_without_extending_deadline(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    events: list[str] = []
    _install_connection_modules(monkeypatch, events)
    deadlines: list[float] = []

    async def timeout_wait_until(awaitable: Any, deadline: float) -> Any:
        deadlines.append(deadline)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(transport, "_async_wait_until", timeout_wait_until)
    caplog.set_level(logging.DEBUG, logger=transport.__name__)

    with pytest.raises(GoveeBleClientError, match="Timed out establishing"):
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=42.0,
        )

    assert deadlines == [42.0]
    assert events == ["lookup"]
    assert "BLE connection timed out while closing stale connections" in caplog.text


@pytest.mark.asyncio
async def test_connection_timeout_includes_reachability_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Connection errors explain the scanner path Home Assistant selected."""

    events: list[str] = []
    _install_connection_modules(monkeypatch, events)

    async def timeout_wait_until(awaitable: Any, _deadline: float) -> Any:
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(transport, "_async_wait_until", timeout_wait_until)
    connection_intent = object()
    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "async_ble_device_from_address": lambda *_args, **_kwargs: SimpleNamespace(
                    name="Purifier"
                ),
                "BluetoothReachabilityIntent": SimpleNamespace(
                    CONNECTION=connection_intent
                ),
                "async_address_reachability_diagnostics": (
                    lambda hass, address, intent: (
                        "adapter hci0, RSSI -72 dBm, 1/5 connection slots"
                        if hass is not None
                        and address == "AA:BB:CC:DD:EE:FF"
                        and intent is connection_intent
                        else "unexpected diagnostics arguments"
                    )
                ),
            }
        },
    )

    with pytest.raises(
        GoveeBleClientError,
        match="adapter hci0, RSSI -72 dBm, 1/5 connection slots",
    ):
        await transport.async_establish_connection(
            object(),
            "AA:BB:CC:DD:EE:FF",
            lambda _client: None,
            deadline=42.0,
        )


@pytest.mark.asyncio
async def test_noncooperative_connection_attempt_does_not_extend_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proxy connection attempt that ignores cancellation cannot block callers."""

    events: list[str] = []
    _install_connection_modules(monkeypatch, events)
    started = asyncio.Event()
    release = asyncio.Event()

    async def establish_connection(**_kwargs: Any) -> FakeClient:
        events.append("establish")
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return FakeClient(events)

    install_modules(
        monkeypatch,
        {
            "bleak_retry_connector": {
                "BleakClientWithServiceCache": object,
                "close_stale_connections": lambda _device: asyncio.sleep(0),
                "establish_connection": establish_connection,
            },
            "homeassistant.components.bluetooth": {
                "async_ble_device_from_address": lambda *_args, **_kwargs: SimpleNamespace(
                    name="Purifier"
                ),
            },
        },
    )

    deadline = asyncio.get_running_loop().time() + 0.01
    with pytest.raises(GoveeBleClientError, match="Timed out establishing"):
        await transport.async_establish_connection(
            object(), "AA:BB:CC:DD:EE:FF", lambda _client: None, deadline=deadline
        )
    await asyncio.wait_for(started.wait(), timeout=1)

    release.set()
    await asyncio.sleep(0.01)
    assert events == ["establish", "disconnect"]


@pytest.mark.asyncio
async def test_connection_preparation_skips_wait_for_an_existing_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def async_scanner_devices_by_address(*args: Any, **kwargs: Any) -> list[object]:
        events.append("paths")
        return [object()]

    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        events.append("wait")
        raise AssertionError("fresh advertisement wait should be skipped")

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": lambda *args, **kwargs: None,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": async_scanner_devices_by_address,
            },
        },
    )

    await transport.async_prepare_connection_path(
        object(), "AA:BB:CC:DD:EE:FF", after=None
    )

    assert events == ["paths"]


@pytest.mark.asyncio
async def test_connection_preparation_waits_for_a_fresh_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    path_results = [[], [object()]]
    process_calls: list[tuple[dict[str, Any], Any, int]] = []
    service_info = SimpleNamespace(time=42.1)

    def async_scanner_devices_by_address(*args: Any, **kwargs: Any) -> list[object]:
        events.append("paths")
        return path_results.pop(0)

    def async_clear_advertisement_history(*args: Any, **kwargs: Any) -> None:
        events.append("clear")

    async def async_process_advertisements(
        _hass: Any,
        callback: Any,
        match_dict: dict[str, Any],
        mode: Any,
        timeout: int,
    ) -> Any:
        events.append("wait")
        process_calls.append((match_dict, mode, timeout))
        assert callback(SimpleNamespace(time=41.9)) is False
        assert callback(service_info) is True
        return service_info

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": async_clear_advertisement_history,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": async_scanner_devices_by_address,
            },
        },
    )

    await transport.async_prepare_connection_path(
        object(), "AA:BB:CC:DD:EE:FF", after=42.0
    )

    assert events == ["paths", "clear", "wait", "paths"]
    assert process_calls == [
        (
            {"address": "AA:BB:CC:DD:EE:FF", "connectable": True},
            "active",
            transport.FRESH_ADVERTISEMENT_TIMEOUT,
        )
    ]


@pytest.mark.asyncio
async def test_connection_preparation_waits_for_path_inventory_after_advertisement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    path_results = [[], [], [object()]]

    def async_scanner_devices_by_address(*args: Any, **kwargs: Any) -> list[object]:
        events.append("paths")
        return path_results.pop(0)

    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        events.append("wait")
        return SimpleNamespace(time=42.1)

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": lambda *args, **kwargs: None,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": async_scanner_devices_by_address,
            },
        },
    )
    monkeypatch.setattr(transport, "FRESH_ADVERTISEMENT_POLL_INTERVAL", 0)

    await transport.async_prepare_connection_path(
        object(), "AA:BB:CC:DD:EE:FF", after=42.0
    )

    assert events == ["paths", "wait", "paths", "paths"]


@pytest.mark.asyncio
async def test_connection_preparation_keeps_automatic_scan_active(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    active_scan_started = asyncio.Event()
    allow_active_scan_to_finish = asyncio.Event()
    active_scan_finished = asyncio.Event()
    durations: list[float] = []
    task_names: list[str] = []
    path_results = [[], [object()]]

    class FakeHass:
        def async_create_background_task(
            self, target: Any, name: str
        ) -> asyncio.Task[Any]:
            task_names.append(name)
            return asyncio.create_task(target)

    def async_scanner_devices_by_address(*args: Any, **kwargs: Any) -> list[object]:
        return path_results.pop(0)

    async def async_request_active_scan(_hass: Any, duration: float) -> None:
        durations.append(duration)
        active_scan_started.set()
        try:
            await allow_active_scan_to_finish.wait()
        finally:
            active_scan_finished.set()

    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        await active_scan_started.wait()
        return SimpleNamespace(time=42.1)

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": lambda *args, **kwargs: None,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_request_active_scan": async_request_active_scan,
                "async_scanner_devices_by_address": async_scanner_devices_by_address,
            },
        },
    )

    await transport.async_prepare_connection_path(
        FakeHass(), "AA:BB:CC:DD:EE:FF", after=42.0
    )

    assert durations == [transport.RECOVERY_ACTIVE_SCAN_DURATION]
    assert task_names == ["Govee BLE recovery active scan"]
    assert active_scan_finished.is_set() is False
    assert transport._ACTIVE_SCAN_TASKS

    allow_active_scan_to_finish.set()
    await active_scan_finished.wait()
    await asyncio.sleep(0)
    assert not transport._ACTIVE_SCAN_TASKS


@pytest.mark.asyncio
async def test_connection_preparation_accepts_a_cached_fresh_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    def async_scanner_devices_by_address(*args: Any, **kwargs: Any) -> list[object]:
        events.append("paths")
        return [object()]

    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        events.append("wait")
        raise AssertionError("cached fresh advertisement should be accepted")

    async def async_request_active_scan(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("cached path should not require a mode transition")

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_last_service_info": lambda *args, **kwargs: SimpleNamespace(
                    time=42.1
                ),
                "async_process_advertisements": async_process_advertisements,
                "async_request_active_scan": async_request_active_scan,
                "async_scanner_devices_by_address": async_scanner_devices_by_address,
            },
        },
    )

    await transport.async_prepare_connection_path(
        object(), "AA:BB:CC:DD:EE:FF", after=42.0
    )

    assert events == ["paths"]


@pytest.mark.asyncio
async def test_connection_preparation_preserves_an_existing_legacy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    latest_results = [
        SimpleNamespace(time=41.9),
        SimpleNamespace(time=42.1),
    ]

    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        events.append("wait")
        raise AssertionError("legacy Home Assistant path should remain usable")

    def async_last_service_info(*args: Any, **kwargs: Any) -> Any:
        events.append("latest")
        return latest_results.pop(0)

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_last_service_info": async_last_service_info,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": lambda *args, **kwargs: [object()],
            },
        },
    )

    await transport.async_prepare_connection_path(
        object(), "AA:BB:CC:DD:EE:FF", after=42.0
    )

    assert events == ["latest", "latest"]


@pytest.mark.asyncio
async def test_connection_preparation_timeout_has_a_precise_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": lambda *args, **kwargs: None,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": lambda *args, **kwargs: [],
            },
        },
    )

    with pytest.raises(
        GoveeBleClientError,
        match="Timed out waiting for a fresh purifier advertisement",
    ):
        await transport.async_prepare_connection_path(
            object(), "AA:BB:CC:DD:EE:FF", after=42.0
        )


@pytest.mark.asyncio
async def test_connection_preparation_can_defer_another_advertisement_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("advertisement wait should be backed off")

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": lambda *args, **kwargs: [],
            },
        },
    )

    with pytest.raises(
        GoveeBleClientError, match="No fresh connectable Bluetooth path"
    ):
        await transport.async_prepare_connection_path(
            object(),
            "AA:BB:CC:DD:EE:FF",
            after=42.0,
            wait_for_advertisement=False,
        )


@pytest.mark.asyncio
async def test_fresh_advertisement_without_a_path_has_a_precise_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def async_process_advertisements(*args: Any, **kwargs: Any) -> Any:
        return SimpleNamespace(time=42.1)

    install_modules(
        monkeypatch,
        {
            "homeassistant.components.bluetooth": {
                "BluetoothScanningMode": SimpleNamespace(ACTIVE="active"),
                "async_clear_advertisement_history": lambda *args, **kwargs: None,
                "async_last_service_info": lambda *args, **kwargs: None,
                "async_process_advertisements": async_process_advertisements,
                "async_scanner_devices_by_address": lambda *args, **kwargs: [],
            },
        },
    )
    monkeypatch.setattr(transport, "FRESH_ADVERTISEMENT_TIMEOUT", 0)

    with pytest.raises(
        GoveeBleClientError, match="No connectable Bluetooth path was found"
    ):
        await transport.async_prepare_connection_path(
            object(), "AA:BB:CC:DD:EE:FF", after=42.0
        )


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
