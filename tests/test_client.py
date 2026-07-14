import asyncio
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.client import (
    GoveeBleClient,
    GoveeBleClientError,
)
from custom_components.govee_ble_air_purifier.coordinator import GoveeData
from custom_components.govee_ble_air_purifier.protocol import build_frame
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

    assert await client.async_get_state() == GoveeData(
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

    assert await client.async_get_state() == GoveeData(
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
async def test_production_connection_wrapper_does_not_mask_cleanup_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBleakClient(stall_stop_notify=True)

    async def close_stale_connections(_device: Any) -> None:
        return None

    async def establish_connection(**_kwargs: Any) -> FakeBleakClient:
        return fake

    bleak_module = ModuleType("bleak_retry_connector")
    bleak_module.BleakClientWithServiceCache = object
    bleak_module.close_stale_connections = close_stale_connections
    bleak_module.establish_connection = establish_connection
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_ble_device_from_address = lambda *args, **kwargs: SimpleNamespace(
        name="Purifier"
    )
    components_module = ModuleType("homeassistant.components")
    components_module.bluetooth = bluetooth_module
    homeassistant_module = ModuleType("homeassistant")
    homeassistant_module.components = components_module
    monkeypatch.setitem(__import__("sys").modules, "bleak_retry_connector", bleak_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant", homeassistant_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.components", components_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "homeassistant.components.bluetooth",
        bluetooth_module,
    )
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")

    assert await client._async_write_and_wait(
        H7124_PROFILE.power_on_command,
        H7124_PROFILE.is_power_state_response,
        timeout=0.01,
    )


@pytest.mark.asyncio
async def test_disconnect_cleanup_error_does_not_mask_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBleakClient(fail_disconnect=True)

    async def close_stale_connections(_device: Any) -> None:
        return None

    async def establish_connection(**_kwargs: Any) -> FakeBleakClient:
        return fake

    bleak_module = ModuleType("bleak_retry_connector")
    bleak_module.BleakClientWithServiceCache = object
    bleak_module.close_stale_connections = close_stale_connections
    bleak_module.establish_connection = establish_connection
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_ble_device_from_address = lambda *args, **kwargs: SimpleNamespace(
        name="Purifier"
    )
    components_module = ModuleType("homeassistant.components")
    components_module.bluetooth = bluetooth_module
    homeassistant_module = ModuleType("homeassistant")
    homeassistant_module.components = components_module
    monkeypatch.setitem(__import__("sys").modules, "bleak_retry_connector", bleak_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant", homeassistant_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.components", components_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "homeassistant.components.bluetooth",
        bluetooth_module,
    )

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")

    with pytest.raises(RuntimeError, match="primary failed"):
        await client._async_with_connection(
            lambda _client: (_ for _ in ()).throw(RuntimeError("primary failed"))
        )
    assert fake.disconnected is True


@pytest.mark.asyncio
async def test_stalled_disconnect_is_bounded_and_preserves_primary_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import custom_components.govee_ble_air_purifier.client as client_module

    fake = FakeBleakClient(stall_disconnect=True)

    async def close_stale_connections(_device: Any) -> None:
        return None

    async def establish_connection(**_kwargs: Any) -> FakeBleakClient:
        return fake

    bleak_module = ModuleType("bleak_retry_connector")
    bleak_module.BleakClientWithServiceCache = object
    bleak_module.close_stale_connections = close_stale_connections
    bleak_module.establish_connection = establish_connection
    bluetooth_module = ModuleType("homeassistant.components.bluetooth")
    bluetooth_module.async_ble_device_from_address = lambda *args, **kwargs: SimpleNamespace(
        name="Purifier"
    )
    components_module = ModuleType("homeassistant.components")
    components_module.bluetooth = bluetooth_module
    homeassistant_module = ModuleType("homeassistant")
    homeassistant_module.components = components_module
    monkeypatch.setitem(__import__("sys").modules, "bleak_retry_connector", bleak_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant", homeassistant_module)
    monkeypatch.setitem(__import__("sys").modules, "homeassistant.components", components_module)
    monkeypatch.setitem(
        __import__("sys").modules,
        "homeassistant.components.bluetooth",
        bluetooth_module,
    )
    monkeypatch.setattr(client_module, "DEFAULT_TIMEOUT", 0.01)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")

    with pytest.raises(RuntimeError, match="primary failed"):
        await asyncio.wait_for(
            client._async_with_connection(
                lambda _client: (_ for _ in ()).throw(RuntimeError("primary failed"))
            ),
            0.1,
        )

    assert fake.disconnect_started is True


@pytest.mark.asyncio
async def test_extra_notification_after_batch_completion_is_ignored() -> None:
    fake = FakeBleakClient(send_extra_on_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await client.async_get_state() == GoveeData(
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
