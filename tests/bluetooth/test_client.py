import asyncio
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth.client import (
    GoveeBleClient,
    GoveeBleClientError,
    connection_idle_timeout_for_polling_interval,
)
from custom_components.govee_ble_air_purifier.bluetooth.framing import build_frame
from custom_components.govee_ble_air_purifier.models import PurifierState
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE


class FakeBleakClient:
    def __init__(
        self,
        *,
        fail_disconnect: bool = False,
        fail_stop_notify: bool = False,
        send_extra_on_stop_notify: bool = False,
        send_responses: bool = True,
        stall_start_notify: bool = False,
        stall_write: bool = False,
        stall_stop_notify: bool = False,
        stall_disconnect: bool = False,
        stage_delay: float = 0,
    ) -> None:
        self.fail_disconnect = fail_disconnect
        self.fail_stop_notify = fail_stop_notify
        self.send_extra_on_stop_notify = send_extra_on_stop_notify
        self.send_responses = send_responses
        self.stall_start_notify = stall_start_notify
        self.stall_write = stall_write
        self.stall_stop_notify = stall_stop_notify
        self.stall_disconnect = stall_disconnect
        self.stage_delay = stage_delay
        self.is_connected = True
        self.disconnected = False
        self.disconnect_started = False
        self.notify_handler = None
        self.started_notify: list[str] = []
        self.stopped_notify: list[str] = []
        self.writes: list[tuple[str, bytes, bool]] = []

    async def start_notify(self, char_uuid: str, handler: Any) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_start_notify:
            await asyncio.Event().wait()
        self.started_notify.append(char_uuid)
        self.notify_handler = handler

    async def stop_notify(self, char_uuid: str) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_stop_notify:
            await asyncio.Event().wait()
        if self.send_extra_on_stop_notify and self.notify_handler is not None:
            self.notify_handler(None, build_frame(bytes.fromhex("aa 01 01")))
        self.stopped_notify.append(char_uuid)
        if self.fail_stop_notify:
            raise RuntimeError("cleanup failed")

    async def disconnect(self) -> None:
        await asyncio.sleep(self.stage_delay)
        self.disconnect_started = True
        if self.stall_disconnect:
            await asyncio.Event().wait()
        self.is_connected = False
        self.disconnected = True
        if self.fail_disconnect:
            raise RuntimeError("disconnect failed")

    async def write_gatt_char(
        self, char_uuid: str, command: bytes, *, response: bool
    ) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_write:
            await asyncio.Event().wait()
        self.writes.append((char_uuid, command, response))
        if self.notify_handler is None or not self.send_responses:
            return
        if command == H7124_PROFILE.state_query_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
            )
        if command == H7124_PROFILE.status_query_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55"))
            )
        if command == H7124_PROFILE.power_on_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
            )
        if command == H7124_PROFILE.power_off_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
            )
        if command in H7124_PROFILE.fan_mode_commands.values():
            self.notify_handler(None, command)


class _TestableGoveeBleClient(GoveeBleClient):
    def __init__(self, fake_client: FakeBleakClient) -> None:
        super().__init__(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
        self.fake_client = fake_client
        self.connection_count = 0

    async def _async_with_connection(
        self, operation: Any, *, deadline: float | None = None
    ) -> Any:
        self.connection_count += 1
        return await operation(self.fake_client)


class _RecordingTimeoutClient(GoveeBleClient):
    def __init__(self) -> None:
        super().__init__(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
        self.timeout: float | None = None

    async def _async_write_and_wait_many(
        self,
        requests: tuple[tuple[bytes, Any], ...],
        *,
        timeout: float = 10.0,
    ) -> tuple[bytes, ...]:
        self.timeout = timeout
        return (
            build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01")),
            build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55")),
        )


@pytest.mark.asyncio
async def test_get_state_batches_power_and_status_in_one_subscription() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.state_query_command, False),
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.status_query_command, False),
    ]


@pytest.mark.asyncio
async def test_get_state_uses_shorter_poll_timeout() -> None:
    client = _RecordingTimeoutClient()

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )
    assert client.timeout == 5.0


@pytest.mark.asyncio
async def test_stop_notify_cleanup_error_does_not_mask_timeout() -> None:
    fake = FakeBleakClient(fail_stop_notify=True, send_responses=False)
    client = _TestableGoveeBleClient(fake)

    with pytest.raises(GoveeBleClientError, match="Timed out"):
        await client._async_write_and_wait(
            H7124_PROFILE.status_query_command,
            H7124_PROFILE.is_status_response,
            timeout=0.01,
        )


@pytest.mark.asyncio
async def test_stop_notify_cleanup_error_does_not_fail_successful_command() -> None:
    fake = FakeBleakClient(fail_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_power(True) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stall",
    ["stall_start_notify", "stall_write"],
)
async def test_notification_transaction_stages_are_bounded(stall: str) -> None:
    fake = FakeBleakClient(**{stall: True})
    client = _TestableGoveeBleClient(fake)

    with pytest.raises(GoveeBleClientError, match="Timed out"):
        await asyncio.wait_for(
            client._async_write_and_wait(
                H7124_PROFILE.power_on_command,
                H7124_PROFILE.is_power_state_response,
                timeout=0.01,
            ),
            0.1,
        )


@pytest.mark.asyncio
async def test_notification_transaction_uses_one_timeout_budget() -> None:
    fake = FakeBleakClient(stage_delay=0.02)
    client = _TestableGoveeBleClient(fake)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(GoveeBleClientError, match="Timed out"):
        await client._async_write_and_wait(
            H7124_PROFILE.power_on_command,
            H7124_PROFILE.is_power_state_response,
            timeout=0.03,
        )

    assert loop.time() - started < 0.05


@pytest.mark.asyncio
async def test_notification_transaction_timeout_includes_lock_wait() -> None:
    client = _TestableGoveeBleClient(FakeBleakClient())
    await client._lock.acquire()
    try:
        with pytest.raises(GoveeBleClientError, match="Timed out"):
            await asyncio.wait_for(
                client._async_write_and_wait(
                    H7124_PROFILE.power_on_command,
                    H7124_PROFILE.is_power_state_response,
                    timeout=0.01,
                ),
                0.1,
            )
    finally:
        client._lock.release()


@pytest.mark.asyncio
async def test_stalled_stop_notify_is_bounded_without_failing_success() -> None:
    fake = FakeBleakClient(stall_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await asyncio.wait_for(
        client._async_write_and_wait(
            H7124_PROFILE.power_on_command,
            H7124_PROFILE.is_power_state_response,
            timeout=0.01,
        ),
        0.1,
    )


@pytest.mark.asyncio
async def test_extra_notification_after_batch_completion_is_ignored() -> None:
    fake = FakeBleakClient(send_extra_on_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )


@pytest.mark.asyncio
async def test_power_and_mode_command_is_batched_in_one_connection() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    await client.async_set_power_and_fan_mode("Sleep")

    assert client.connection_count == 1
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.power_on_command, False),
        (
            H7124_PROFILE.write_char_uuid,
            H7124_PROFILE.fan_mode_commands["Sleep"],
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_power_command_waits_for_aa01_confirmation() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_power(True) is True

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.power_on_command, False),
    ]


@pytest.mark.asyncio
async def test_fan_mode_command_waits_for_exact_echo_confirmation() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_fan_mode("Low") == "Low"

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.fan_mode_commands["Low"], False),
    ]


@pytest.mark.asyncio
async def test_connection_is_established_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    hass = object()
    client = GoveeBleClient(hass, "AA:BB:CC:DD:EE:FF")
    fake = FakeBleakClient()
    callbacks: list[Any] = []
    calls: list[tuple[Any, str, float]] = []
    disconnects: list[tuple[Any, float]] = []

    async def operation(passed_client: Any) -> str:
        assert passed_client is fake
        return "result"

    async def async_establish_connection(
        passed_hass: Any,
        address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        calls.append((passed_hass, address, deadline))
        callbacks.append(disconnected_callback)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append((passed_client, deadline))
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    deadline = asyncio.get_running_loop().time() + 4.0

    assert await client._async_with_connection(operation, deadline=deadline) == "result"
    assert await client._async_with_connection(operation, deadline=deadline) == "result"

    assert calls == [(hass, "AA:BB:CC:DD:EE:FF", deadline)]
    assert len(callbacks) == 1
    assert disconnects == []

    await client.async_close()

    assert len(disconnects) == 1
    assert disconnects[0][0] is fake


@pytest.mark.parametrize(
    ("polling_interval", "expected_timeout"),
    [
        (5, 10.0),
        (10, 15.0),
        (20, 25.0),
        (25, 30.0),
        (30, 5.0),
        (60, 5.0),
        (300, 5.0),
    ],
)
def test_connection_idle_timeout_adapts_to_polling_interval(
    polling_interval: int, expected_timeout: float
) -> None:
    assert (
        connection_idle_timeout_for_polling_interval(polling_interval)
        == expected_timeout
    )


@pytest.mark.asyncio
async def test_connection_delegate_creates_default_deadline_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import client as client_module
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    deadlines: list[float] = []

    fake = FakeBleakClient()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        deadlines.append(deadline)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(client_module, "DEFAULT_TIMEOUT", 7.0)
    loop = asyncio.get_running_loop()
    before = loop.time()

    async def operation(_client: Any) -> None:
        return None

    await client._async_with_connection(operation)

    after = loop.time()
    assert len(deadlines) == 1
    assert before + 7.0 <= deadlines[0] <= after + 7.0

    await client.async_close()


@pytest.mark.asyncio
async def test_disconnected_callback_reconnects_and_ignores_stale_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    callbacks: list[Any] = []
    establish_count = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        callbacks.append(disconnected_callback)
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    async def operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client._async_with_connection(operation) is connected_clients[0]
    connected_clients[0].is_connected = False
    callbacks[0](connected_clients[0])
    assert await client._async_with_connection(operation) is connected_clients[1]

    callbacks[0](connected_clients[0])
    assert await client._async_with_connection(operation) is connected_clients[1]
    assert establish_count == 2

    await client.async_close()


@pytest.mark.asyncio
async def test_operation_failure_invalidates_connection_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    establish_count = 0
    disconnects: list[Any] = []

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append(passed_client)
        await passed_client.disconnect()

    attempts = 0

    async def failing_operation(_client: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("operation failed")

    async def successful_operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    with pytest.raises(ValueError, match="operation failed"):
        await client._async_with_connection(failing_operation)

    assert attempts == 1
    assert disconnects == [connected_clients[0]]
    assert await client._async_with_connection(successful_operation) is connected_clients[1]

    await client.async_close()


@pytest.mark.asyncio
async def test_idle_timeout_disconnects_and_next_operation_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import client as client_module
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    monkeypatch.setattr(client_module, "CONNECTION_IDLE_GRACE", 0.0)
    client = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:FF", polling_interval_seconds=60
    )
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    establish_count = 0
    disconnected = asyncio.Event()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()
        disconnected.set()

    async def operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client._async_with_connection(operation) is connected_clients[0]
    await asyncio.wait_for(disconnected.wait(), 0.1)
    assert connected_clients[0].is_connected is False

    assert await client._async_with_connection(operation) is connected_clients[1]
    assert establish_count == 2

    await client.async_close()


@pytest.mark.asyncio
async def test_notification_cleanup_failure_preserves_result_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected_clients = [
        FakeBleakClient(fail_stop_notify=True),
        FakeBleakClient(),
    ]
    establish_count = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client.async_set_power(True) is True
    assert connected_clients[0].disconnected is True
    assert await client.async_set_power(True) is True
    assert establish_count == 2

    await client.async_close()


@pytest.mark.asyncio
async def test_close_waits_for_active_transaction_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected = FakeBleakClient()

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client._client = connected
    await client._lock.acquire()

    close_task = asyncio.create_task(client.async_close())
    await asyncio.sleep(0)

    assert close_task.done() is False
    assert connected.disconnected is False

    client._lock.release()
    await close_task

    assert connected.disconnected is True
