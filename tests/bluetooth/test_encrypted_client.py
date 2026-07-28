import asyncio
import logging
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import client as client_module
from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleClientError,
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth.client import GoveeBleClient
from custom_components.govee_ble_air_purifier.bluetooth.framing import (
    ProtocolError,
    build_frame,
)
from custom_components.govee_ble_air_purifier.bluetooth.govee_v1 import (
    COMMUNICATION_KEY,
    decrypt_frame,
    encrypt_frame,
)
from custom_components.govee_ble_air_purifier.models import PurifierState
from custom_components.govee_ble_air_purifier.profiles import get_profile

H7129_PROFILE = get_profile("h7129")
SESSION_KEY_1 = bytes.fromhex("46 73 a0 ce fb 28 56 83 b0 dd 0b 38 65 93 c0 ed")
SESSION_KEY_2 = bytes.fromhex("10 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f")


class EncryptedFakeBleakClient:
    def __init__(
        self,
        session_key: bytes,
        *,
        mismatch_confirmation: bool = False,
        respond_to_handshake: bool = True,
        disconnect_on_application_notify: bool = False,
        delay_disconnect_callback: bool = False,
        withhold_handshake_response: int | None = None,
        withhold_application_response: int | None = None,
        respond_to_application: bool = True,
        corrupt_application_response: bool = False,
        communication_key_notification_commands: tuple[int, ...] = (),
        application_notifications: tuple[bytes, ...] = (),
    ) -> None:
        self.session_key = session_key
        self.mismatch_confirmation = mismatch_confirmation
        self.respond_to_handshake = respond_to_handshake
        self.disconnect_on_application_notify = disconnect_on_application_notify
        self.delay_disconnect_callback = delay_disconnect_callback
        self.withhold_handshake_response = withhold_handshake_response
        self.withhold_application_response = withhold_application_response
        self.respond_to_application = respond_to_application
        self.corrupt_application_response = corrupt_application_response
        self.communication_key_notification_commands = (
            communication_key_notification_commands
        )
        self.application_notifications = application_notifications
        self.is_connected = True
        self.disconnected = False
        self.notify_handler: Any = None
        self.started_notify: list[str] = []
        self.stopped_notify: list[str] = []
        self.writes: list[tuple[str, bytes, bool]] = []
        self.handshake_frames: list[bytes] = []
        self.application_frames: list[bytes] = []
        self.session_established = False
        self.disconnected_callback: Any = None
        self.application_write_event = asyncio.Event()

    async def start_notify(self, char_uuid: str, handler: Any) -> None:
        if self.session_established and self.disconnect_on_application_notify:
            assert self.disconnected_callback is not None
            if self.delay_disconnect_callback:
                callback = self.disconnected_callback

                def disconnect_later() -> None:
                    self.is_connected = False
                    callback(self)

                asyncio.get_running_loop().call_soon(disconnect_later)
            else:
                self.is_connected = False
                self.disconnected_callback(self)
            raise RuntimeError("link disconnected")
        self.started_notify.append(char_uuid)
        self.notify_handler = handler

    async def stop_notify(self, char_uuid: str) -> None:
        self.stopped_notify.append(char_uuid)
        self.notify_handler = None

    async def disconnect(self) -> None:
        self.is_connected = False
        self.disconnected = True

    async def write_gatt_char(
        self, char_uuid: str, command: bytes, *, response: bool
    ) -> None:
        self.writes.append((char_uuid, command, response))
        if not self.session_established:
            await self._handle_handshake_write(command)
            return

        plaintext = decrypt_frame(command, self.session_key)
        self.application_frames.append(plaintext)
        self.application_write_event.set()
        if self.withhold_application_response == len(self.application_frames):
            return
        if self.notify_handler is None:
            return
        for notification in self.application_notifications:
            self.notify_handler(None, encrypt_frame(notification, self.session_key))
        for command in self.communication_key_notification_commands:
            late_frame = build_frame(bytes((0xE7, command)) + bytes(range(17)))
            self.notify_handler(
                None, encrypt_frame(late_frame, COMMUNICATION_KEY)
            )
        if not self.respond_to_application:
            return
        response_frame = self._response_for(plaintext)
        if response_frame is not None:
            wire_response = encrypt_frame(response_frame, self.session_key)
            if self.corrupt_application_response:
                wire_response = wire_response[:-1] + bytes((wire_response[-1] ^ 1,))
            self.notify_handler(None, wire_response)

    async def _handle_handshake_write(self, command: bytes) -> None:
        plaintext = decrypt_frame(command, COMMUNICATION_KEY)
        self.handshake_frames.append(plaintext)
        if self.withhold_handshake_response == plaintext[1]:
            return
        if not self.respond_to_handshake or self.notify_handler is None:
            return
        if plaintext[:2] == b"\xe7\x01":
            response = build_frame(b"\xe7\x01" + self.session_key + b"\x00")
        elif plaintext[:2] == b"\xe7\x02":
            response = plaintext
            if self.mismatch_confirmation:
                response = build_frame(b"\xe7\x02" + bytes([0xFF]) * 17)
            else:
                self.session_established = True
        else:
            return
        self.notify_handler(None, encrypt_frame(response, COMMUNICATION_KEY))

    @staticmethod
    def _response_for(command: bytes) -> bytes | None:
        if command == H7129_PROFILE.state_query_command:
            return build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
        if command == H7129_PROFILE.status_query_command:
            return build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55"))
        if command == H7129_PROFILE.power_on_command:
            return build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
        if command == H7129_PROFILE.power_off_command:
            return build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
        if command in H7129_PROFILE.fan_mode_commands.values():
            return command
        return None


def _install_connections(
    monkeypatch: pytest.MonkeyPatch, clients: list[EncryptedFakeBleakClient]
) -> tuple[list[Any], list[EncryptedFakeBleakClient]]:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    callbacks: list[Any] = []
    disconnects: list[EncryptedFakeBleakClient] = []
    connection_index = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> EncryptedFakeBleakClient:
        nonlocal connection_index
        client = clients[connection_index]
        connection_index += 1
        callbacks.append(disconnected_callback)
        client.disconnected_callback = disconnected_callback
        return client

    async def async_disconnect(
        client: EncryptedFakeBleakClient, *, deadline: float
    ) -> None:
        disconnects.append(client)
        await client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    return callbacks, disconnects


def _stage_event(
    monkeypatch: pytest.MonkeyPatch, client: GoveeBleClient, expected_stage: str
) -> asyncio.Event:
    reached = asyncio.Event()
    original_log_stage = client._log_stage

    def log_stage(operation: str, stage: str, started: float, deadline: float) -> None:
        original_log_stage(operation, stage, started, deadline)
        if stage == expected_stage:
            reached.set()

    monkeypatch.setattr(client, "_log_stage", log_stage)
    return reached


def _assert_sensitive_bytes_not_logged(log_text: str, *values: bytes) -> None:
    for value in values:
        representations = {
            value.hex(),
            value.hex().upper(),
            value.hex(" "),
            value.hex(" ").upper(),
            repr(value),
        }
        if all(0x20 <= byte < 0x7F for byte in value):
            representations.add(value.decode("ascii"))
        assert all(
            representation not in log_text for representation in representations
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["handshake", "application"])
async def test_caller_cancel_skips_notify_cleanup_and_drops_client(
    monkeypatch: pytest.MonkeyPatch,
    cancel_phase: str,
) -> None:
    started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    class CancellationResistantClient(EncryptedFakeBleakClient):
        async def start_notify(self, char_uuid: str, handler: Any) -> None:
            phase = "application" if self.session_established else "handshake"
            if phase != cancel_phase:
                await super().start_notify(char_uuid, handler)
                return
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()
                raise RuntimeError(f"late {phase} failure")
            finally:
                finished.set()

    fake = CancellationResistantClient(SESSION_KEY_1)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    operation = asyncio.create_task(client.async_set_power(True))

    try:
        await asyncio.wait_for(started.wait(), 0.5)
        stop_count = len(fake.stopped_notify)
        operation.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((operation,), timeout=0.5)
        assert operation in done
        with pytest.raises(asyncio.CancelledError):
            await operation

        assert finished.is_set() is False
        assert len(fake.stopped_notify) == stop_count
        assert stop_count == (1 if cancel_phase == "application" else 0)
        assert fake.application_frames == []
        assert disconnects == [fake]
        assert fake.disconnected is True
        assert client._client is None
        assert client._disconnect_signal is None
        assert client._session_key is None
        assert client._lock.locked() is False
        assert client_module._ABANDONED_OPERATION_FUTURES
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 0.5)
        await asyncio.gather(operation, return_exceptions=True)
        await asyncio.sleep(0)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_h7129_reuses_shared_protocol_after_one_handshake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )
    for mode in H7129_PROFILE.fan_mode_commands:
        assert await client.async_set_fan_mode(mode) == mode

    assert [frame[:2] for frame in fake.handshake_frames] == [b"\xe7\x01", b"\xe7\x02"]
    assert fake.application_frames == [
        H7129_PROFILE.state_query_command,
        H7129_PROFILE.status_query_command,
        *H7129_PROFILE.fan_mode_commands.values(),
    ]
    assert fake.application_frames[-2] == H7129_PROFILE.fan_mode_commands["Auto"]
    assert all(
        wire != plaintext
        for (_uuid, wire, _response), plaintext in zip(
            fake.writes, fake.handshake_frames + fake.application_frames, strict=True
        )
    )
    assert client._session_key == SESSION_KEY_1

    await client.async_close()

    assert disconnects == [fake]
    assert client._disconnect_signal is None
    assert client._session_key is None


@pytest.mark.asyncio
async def test_h7129_handshake_does_not_consume_application_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1)
    _callbacks, _disconnects = _install_connections(monkeypatch, [fake])
    original_handle_handshake_write = fake._handle_handshake_write

    async def delayed_handle_handshake_write(command: bytes) -> None:
        await asyncio.sleep(0.02)
        await original_handle_handshake_write(command)

    monkeypatch.setattr(fake, "_handle_handshake_write", delayed_handle_handshake_write)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    assert await client._async_write_and_wait(
        H7129_PROFILE.power_on_command,
        H7129_PROFILE.is_power_state_response,
        timeout=0.01,
    )

    await client.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("handshake_command", [0x01, 0x02])
async def test_h7129_disconnect_wakes_handshake_response_wait(
    monkeypatch: pytest.MonkeyPatch,
    handshake_command: int,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1, withhold_handshake_response=handshake_command
    )
    callbacks, _disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    waiting = _stage_event(
        monkeypatch,
        client,
        f"waiting for e7 {handshake_command:02x} response",
    )

    operation = asyncio.create_task(client.async_set_power(True))
    await asyncio.wait_for(waiting.wait(), 0.1)
    fake.is_connected = False
    callbacks[0](fake)
    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await asyncio.wait_for(operation, 0.1)

    assert [frame[1] for frame in fake.handshake_frames] == list(
        range(1, handshake_command + 1)
    )
    assert fake.application_frames == []
    assert fake.stopped_notify == []
    assert client._client is None
    assert client._session_key is None


@pytest.mark.asyncio
@pytest.mark.parametrize("response_number", [1, 2])
async def test_h7129_disconnect_wakes_application_response_wait(
    monkeypatch: pytest.MonkeyPatch,
    response_number: int,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1, withhold_application_response=response_number
    )
    callbacks, _disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    waiting = _stage_event(
        monkeypatch, client, f"waiting for response {response_number}/2"
    )

    operation = asyncio.create_task(
        client._async_write_and_wait_many(
            (
                (
                    H7129_PROFILE.state_query_command,
                    H7129_PROFILE.is_power_state_response,
                ),
                (
                    H7129_PROFILE.status_query_command,
                    H7129_PROFILE.is_status_response,
                ),
            ),
            timeout=5.0,
        )
    )
    await asyncio.wait_for(waiting.wait(), 0.1)
    fake.is_connected = False
    callbacks[0](fake)
    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await asyncio.wait_for(operation, 0.1)

    assert len(fake.application_frames) == response_number
    assert len(fake.stopped_notify) == 1
    assert client._client is None
    assert client._session_key is None


@pytest.mark.asyncio
async def test_h7129_debug_logs_show_transaction_stages_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1)
    _callbacks, _disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    await client.async_get_state()

    assert f"{client._log_label} BLE transaction started with 2 requests" in caplog.text
    assert (
        f"{client._log_label} Govee V1 handshake stage: waiting for e7 01 response"
    ) in caplog.text
    assert (
        f"{client._log_label} Govee V1 handshake stage: waiting for e7 02 response"
    ) in caplog.text
    assert f"{client._log_label} BLE transaction completed" in caplog.text
    assert "AA:BB:CC:DD:EE:FF" not in caplog.text
    assert SESSION_KEY_1.hex() not in caplog.text

    await client.async_close()


@pytest.mark.asyncio
async def test_h7129_reconnect_negotiates_a_new_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = EncryptedFakeBleakClient(SESSION_KEY_1)
    second = EncryptedFakeBleakClient(SESSION_KEY_2)
    callbacks, disconnects = _install_connections(monkeypatch, [first, second])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    assert await client.async_set_power(True) is True
    assert client._session_key == SESSION_KEY_1
    first_disconnect_signal = client._disconnect_signal
    assert first_disconnect_signal is not None

    first.is_connected = False
    callbacks[0](first)
    assert first_disconnect_signal.is_set()
    assert client._disconnect_signal is None
    assert client._session_key is None

    assert await client.async_set_power(False) is False
    assert client._disconnect_signal is not first_disconnect_signal
    assert client._session_key == SESSION_KEY_2
    assert len(first.handshake_frames) == len(second.handshake_frames) == 2

    callbacks[0](first)
    assert client._session_key == SESSION_KEY_2

    await client.async_close()
    assert disconnects == [second]


@pytest.mark.asyncio
async def test_h7129_poll_retries_with_fresh_session_after_response_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    first = EncryptedFakeBleakClient(SESSION_KEY_1, withhold_application_response=2)
    second = EncryptedFakeBleakClient(SESSION_KEY_2)
    callbacks, disconnects = _install_connections(monkeypatch, [first, second])
    prepare_after: list[float | None] = []

    async def async_prepare_connection_path(
        _hass: Any,
        _address: str,
        *,
        after: float | None,
        wait_for_advertisement: bool = True,
    ) -> None:
        prepare_after.append(after)

    monkeypatch.setattr(
        transport, "async_prepare_connection_path", async_prepare_connection_path
    )
    client = GoveeBleClient(object(), "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    waiting = _stage_event(monkeypatch, client, "waiting for response 2/2")

    operation = asyncio.create_task(client.async_get_state())
    await asyncio.wait_for(waiting.wait(), 0.1)
    first.is_connected = False
    callbacks[0](first)
    assert await asyncio.wait_for(operation, 0.1) == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )

    assert len(callbacks) == 2
    assert [frame[:2] for frame in first.handshake_frames] == [b"\xe7\x01", b"\xe7\x02"]
    assert [frame[:2] for frame in second.handshake_frames] == [
        b"\xe7\x01",
        b"\xe7\x02",
    ]
    assert first.application_frames == [
        H7129_PROFILE.state_query_command,
        H7129_PROFILE.status_query_command,
    ]
    assert second.application_frames == [
        H7129_PROFILE.state_query_command,
        H7129_PROFILE.status_query_command,
    ]
    assert prepare_after[0] is None
    assert prepare_after[1] is not None
    assert client._session_key == SESSION_KEY_2

    await client.async_close()
    assert disconnects == [second]


@pytest.mark.asyncio
@pytest.mark.parametrize("delay_disconnect_callback", [False, True])
async def test_h7129_poll_recovers_from_disconnect_during_start_notify(
    monkeypatch: pytest.MonkeyPatch,
    delay_disconnect_callback: bool,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    first = EncryptedFakeBleakClient(
        SESSION_KEY_1,
        disconnect_on_application_notify=True,
        delay_disconnect_callback=delay_disconnect_callback,
    )
    second = EncryptedFakeBleakClient(SESSION_KEY_2)
    callbacks, disconnects = _install_connections(monkeypatch, [first, second])
    prepare_calls: list[float | None] = []

    async def async_prepare_connection_path(
        _hass: Any,
        _address: str,
        *,
        after: float | None,
        wait_for_advertisement: bool = True,
    ) -> None:
        prepare_calls.append(after)

    monkeypatch.setattr(
        transport, "async_prepare_connection_path", async_prepare_connection_path
    )
    client = GoveeBleClient(object(), "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )

    assert len(callbacks) == 2
    assert len(first.handshake_frames) == len(second.handshake_frames) == 2
    assert first.application_frames == []
    assert second.application_frames == [
        H7129_PROFILE.state_query_command,
        H7129_PROFILE.status_query_command,
    ]
    assert prepare_calls[0] is None
    assert prepare_calls[1] is not None
    assert client._session_key == SESSION_KEY_2

    await client.async_close()
    assert disconnects == [second]


@pytest.mark.asyncio
async def test_h7129_command_does_not_replay_after_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    first = EncryptedFakeBleakClient(
        SESSION_KEY_1, disconnect_on_application_notify=True
    )
    callbacks, _disconnects = _install_connections(monkeypatch, [first])

    async def async_prepare_connection_path(
        _hass: Any,
        _address: str,
        *,
        after: float | None,
        wait_for_advertisement: bool = True,
    ) -> None:
        return None

    monkeypatch.setattr(
        transport, "async_prepare_connection_path", async_prepare_connection_path
    )
    client = GoveeBleClient(object(), "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await client.async_set_power(True)

    assert len(callbacks) == 1
    assert first.application_frames == []


@pytest.mark.asyncio
async def test_h7129_command_does_not_replay_after_confirmation_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = EncryptedFakeBleakClient(SESSION_KEY_1, withhold_application_response=1)
    callbacks, _disconnects = _install_connections(monkeypatch, [first])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    waiting = _stage_event(monkeypatch, client, "waiting for response 1/1")

    operation = asyncio.create_task(client.async_set_power(True))
    await asyncio.wait_for(waiting.wait(), 0.1)
    first.is_connected = False
    callbacks[0](first)
    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await asyncio.wait_for(operation, 0.1)

    assert len(callbacks) == 1
    assert first.application_frames == [H7129_PROFILE.power_on_command]


@pytest.mark.asyncio
async def test_stale_disconnect_does_not_wake_replacement_response_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = EncryptedFakeBleakClient(SESSION_KEY_1)
    second = EncryptedFakeBleakClient(SESSION_KEY_2, respond_to_application=False)
    callbacks, _disconnects = _install_connections(monkeypatch, [first, second])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    assert await client.async_set_power(True) is True
    first.is_connected = False
    callbacks[0](first)

    waiting = _stage_event(monkeypatch, client, "waiting for response 1/1")
    command = asyncio.create_task(client.async_set_power(False))
    await asyncio.wait_for(waiting.wait(), 0.1)
    callbacks[0](first)
    await asyncio.sleep(0)
    assert command.done() is False

    assert second.notify_handler is not None
    response = build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
    second.notify_handler(None, encrypt_frame(response, SESSION_KEY_2))

    assert await asyncio.wait_for(command, 0.1) is False
    assert client._session_key == SESSION_KEY_2
    await client.async_close()


@pytest.mark.asyncio
async def test_h7129_rejects_mismatched_handshake_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1, mismatch_confirmation=True)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    with pytest.raises(GoveeBleClientError, match="encrypted purifier session"):
        await client.async_set_power(True)

    assert fake.application_frames == []
    assert disconnects == [fake]
    assert client._client is None
    assert client._session_key is None


@pytest.mark.asyncio
async def test_h7129_checksum_failure_discards_session_and_next_call_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = EncryptedFakeBleakClient(SESSION_KEY_1, corrupt_application_response=True)
    second = EncryptedFakeBleakClient(SESSION_KEY_2)
    _callbacks, disconnects = _install_connections(monkeypatch, [first, second])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    with pytest.raises(ProtocolError, match="Invalid checksum"):
        await client.async_set_power(True)

    assert disconnects == [first]
    assert client._client is None
    assert client._disconnect_signal is None
    assert client._session_key is None

    assert await client.async_set_power(False) is False
    assert client._session_key == SESSION_KEY_2
    assert len(first.handshake_frames) == len(second.handshake_frames) == 2
    await client.async_close()


@pytest.mark.asyncio
@pytest.mark.parametrize("handshake_command", [0x01, 0x02])
async def test_h7129_ignores_late_handshake_application_notification(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    handshake_command: int,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1,
        communication_key_notification_commands=(handshake_command,),
    )
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )
    late_plaintext = build_frame(
        bytes((0xE7, handshake_command)) + bytes(range(17))
    )
    late_ciphertext = encrypt_frame(late_plaintext, COMMUNICATION_KEY)
    application_plaintext = EncryptedFakeBleakClient._response_for(
        H7129_PROFILE.power_on_command
    )
    assert application_plaintext is not None
    application_ciphertext = encrypt_frame(application_plaintext, SESSION_KEY_1)

    assert await client.async_set_power(True) is True

    assert (
        f"{client._log_label} Govee V1 application decryption diagnostic: "
        f"ignored valid late e7 {handshake_command:02x} handshake notification"
        in caplog.text
    )
    assert fake.application_frames == [H7129_PROFILE.power_on_command]
    assert len(fake.handshake_frames) == 2
    assert client._session_key == SESSION_KEY_1
    assert disconnects == []
    assert "BLE request 1/1 write completed in" in caplog.text
    assert "BLE response 1/1 received" in caplog.text
    assert (
        "notifications: 2, stale handshakes: 1, nonmatching: 0" in caplog.text
    )
    assert "aa:bb:cc:dd:ee:ff" not in caplog.text.lower()
    _assert_sensitive_bytes_not_logged(
        caplog.text,
        SESSION_KEY_1,
        COMMUNICATION_KEY,
        late_plaintext,
        late_ciphertext,
        application_plaintext,
        application_ciphertext,
    )
    await client.async_close()
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_h7129_ignores_duplicate_late_handshakes_for_each_poll_response(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1, communication_key_notification_commands=(0x02, 0x02)
    )
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )

    ignored_message = (
        f"{client._log_label} Govee V1 application decryption diagnostic: "
        "ignored valid late e7 02 handshake notification"
    )
    assert caplog.text.count(ignored_message) == 4
    assert fake.application_frames == [
        H7129_PROFILE.state_query_command,
        H7129_PROFILE.status_query_command,
    ]
    assert caplog.text.count(
        "notifications: 3, stale handshakes: 2, nonmatching: 0"
    ) == 2
    assert len(fake.handshake_frames) == 2
    assert client._session_key == SESSION_KEY_1
    assert disconnects == []

    await client.async_close()
    assert disconnects == [fake]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("communication_key_commands", "expected_notifications", "expected_stale"),
    [((), 0, 0), ((0x02,), 1, 1)],
)
async def test_h7129_application_timeout_logs_notification_counts(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    communication_key_commands: tuple[int, ...],
    expected_notifications: int,
    expected_stale: int,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1,
        communication_key_notification_commands=communication_key_commands,
        respond_to_application=False,
    )
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    with pytest.raises(
        GoveeBleClientError, match="Timed out waiting for purifier response"
    ):
        await asyncio.wait_for(
            client._async_write_and_wait(
                H7129_PROFILE.power_on_command,
                H7129_PROFILE.is_power_state_response,
                timeout=0.01,
            ),
            0.1,
        )

    if communication_key_commands:
        assert "ignored valid late e7 02 handshake notification" in caplog.text
    assert fake.application_frames == [H7129_PROFILE.power_on_command]
    assert len(fake.writes) == 3
    assert "BLE response timeout diagnostic: request 1/1" in caplog.text
    assert (
        f"notifications: {expected_notifications}, "
        f"stale handshakes: {expected_stale}, nonmatching: 0" in caplog.text
    )
    assert disconnects == [fake]
    assert client._session_key is None


@pytest.mark.asyncio
async def test_h7129_response_diagnostic_counts_nonmatching_notifications(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    nonmatching = build_frame(b"\xee\x05\x03")
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1, application_notifications=(nonmatching,)
    )
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    assert await client.async_set_power(True) is True

    assert (
        "notifications: 2, stale handshakes: 0, nonmatching: 1" in caplog.text
    )
    _assert_sensitive_bytes_not_logged(
        caplog.text,
        nonmatching,
        encrypt_frame(nonmatching, SESSION_KEY_1),
    )
    assert fake.application_frames == [H7129_PROFILE.power_on_command]
    assert disconnects == []

    await client.async_close()
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_h7129_does_not_ignore_other_communication_key_frames(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = EncryptedFakeBleakClient(
        SESSION_KEY_1, communication_key_notification_commands=(0x03,)
    )
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    with pytest.raises(ProtocolError, match="Invalid checksum"):
        await client.async_set_power(True)

    assert (
        "not a valid late e7 01/e7 02 handshake notification" in caplog.text
    )
    assert fake.application_frames == [H7129_PROFILE.power_on_command]
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_h7129_classifies_corrupt_application_notification_as_not_handshake(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1, corrupt_application_response=True)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )
    application_plaintext = EncryptedFakeBleakClient._response_for(
        H7129_PROFILE.power_on_command
    )
    assert application_plaintext is not None
    application_ciphertext = encrypt_frame(application_plaintext, SESSION_KEY_1)
    corrupt_ciphertext = application_ciphertext[:-1] + bytes(
        (application_ciphertext[-1] ^ 1,)
    )

    with pytest.raises(ProtocolError, match="Invalid checksum"):
        await client.async_set_power(True)

    assert (
        f"{client._log_label} Govee V1 application decryption diagnostic: "
        "not a valid late e7 01/e7 02 handshake notification" in caplog.text
    )
    assert "aa:bb:cc:dd:ee:ff" not in caplog.text.lower()
    _assert_sensitive_bytes_not_logged(
        caplog.text,
        SESSION_KEY_1,
        COMMUNICATION_KEY,
        application_plaintext,
        application_ciphertext,
        corrupt_ciphertext,
    )
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_h7129_skips_diagnostic_when_session_key_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    original_write = fake.write_gatt_char

    async def clear_session_before_application_notification(
        char_uuid: str, command: bytes, *, response: bool
    ) -> None:
        if fake.session_established:
            client._session_key = None
        await original_write(char_uuid, command, response=response)

    monkeypatch.setattr(
        fake, "write_gatt_char", clear_session_before_application_notification
    )
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    with pytest.raises(ProtocolError, match="session is unavailable"):
        await client.async_set_power(True)

    assert "Govee V1 application decryption diagnostic" not in caplog.text
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_h7129_handshake_timeout_discards_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import (
        client as client_module,
    )

    fake = EncryptedFakeBleakClient(SESSION_KEY_1, respond_to_handshake=False)
    _callbacks, disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )
    monkeypatch.setattr(client_module, "HANDSHAKE_TIMEOUT", 0.01)

    with pytest.raises(GoveeBleClientError, match="Timed out establishing"):
        await asyncio.wait_for(
            client._async_write_and_wait(
                H7129_PROFILE.power_on_command,
                H7129_PROFILE.is_power_state_response,
                timeout=0.01,
            ),
            0.1,
        )

    assert disconnects == [fake]
    assert client._session_key is None
    assert (
        f"{client._log_label} Govee V1 handshake timed out during "
        "waiting for e7 01 response" in caplog.text
    )


@pytest.mark.asyncio
async def test_h7129_no_response_write_uses_negotiated_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = EncryptedFakeBleakClient(SESSION_KEY_1)
    _callbacks, _disconnects = _install_connections(monkeypatch, [fake])
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7129_PROFILE)

    await client._async_write_without_response(H7129_PROFILE.power_on_command)

    assert fake.application_frames == [H7129_PROFILE.power_on_command]
    await client.async_close()
