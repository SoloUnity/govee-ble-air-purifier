import asyncio
import logging
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleClientError,
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth.client import GoveeBleClient
from custom_components.govee_ble_air_purifier.bluetooth.framing import build_frame
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
    ) -> None:
        self.session_key = session_key
        self.mismatch_confirmation = mismatch_confirmation
        self.respond_to_handshake = respond_to_handshake
        self.disconnect_on_application_notify = disconnect_on_application_notify
        self.delay_disconnect_callback = delay_disconnect_callback
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
        if self.notify_handler is None:
            return
        response_frame = self._response_for(plaintext)
        if response_frame is not None:
            self.notify_handler(None, encrypt_frame(response_frame, self.session_key))

    async def _handle_handshake_write(self, command: bytes) -> None:
        plaintext = decrypt_frame(command, COMMUNICATION_KEY)
        self.handshake_frames.append(plaintext)
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

    assert "H7129 BLE transaction started with 2 requests" in caplog.text
    assert "H7129 Govee V1 handshake stage: waiting for e7 01 response" in caplog.text
    assert "H7129 Govee V1 handshake stage: waiting for e7 02 response" in caplog.text
    assert "H7129 BLE transaction completed" in caplog.text
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

    first.is_connected = False
    callbacks[0](first)
    assert client._session_key is None

    assert await client.async_set_power(False) is False
    assert client._session_key == SESSION_KEY_2
    assert len(first.handshake_frames) == len(second.handshake_frames) == 2

    callbacks[0](first)
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
async def test_h7129_handshake_timeout_discards_connection(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import client as client_module

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
        "H7129 Govee V1 handshake timed out during waiting for e7 01 response"
        in caplog.text
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
