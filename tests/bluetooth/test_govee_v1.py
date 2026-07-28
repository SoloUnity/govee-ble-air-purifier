import pytest

from custom_components.govee_ble_air_purifier.bluetooth.framing import (
    ProtocolError,
    build_frame,
)
from custom_components.govee_ble_air_purifier.bluetooth.govee_v1 import (
    COMMUNICATION_KEY,
    build_handshake_request,
    decrypt_frame,
    encrypt_frame,
    identify_handshake_frame,
    parse_session_key,
    validate_handshake_confirmation,
)

SESSION_KEY = bytes.fromhex("46 73 a0 ce fb 28 56 83 b0 dd 0b 38 65 93 c0 ed")


@pytest.mark.parametrize(
    ("key", "plaintext", "encrypted"),
    [
        (
            COMMUNICATION_KEY,
            "e7 01 f1 79 1d 8b b6 07 d9 c6 0b 25 9c 8c 40 c0 44 bf 1c 0f",
            "3d 7e 00 e5 9f 2a 5e ce 3a 4b 6c 97 5d 34 6b 03 2c 51 5c 50",
        ),
        (
            COMMUNICATION_KEY,
            "e7 01 46 73 a0 ce fb 28 56 83 b0 dd 0b 38 65 93 c0 ed 1c 22",
            "d2 d6 11 36 42 d9 eb 3a 58 50 b3 2f 74 a8 82 d5 a8 03 5c 7d",
        ),
        (
            COMMUNICATION_KEY,
            "e7 02 b3 f5 0f ed 12 35 7b c0 b6 c2 38 01 28 f9 f1 77 33 f4",
            "bd 54 b2 f9 15 86 7a 8a 44 65 e7 22 8c a8 04 de 99 99 73 ab",
        ),
        (
            SESSION_KEY,
            "aa 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ab",
            "e2 66 cf f9 c5 d3 ed fa ab 6f e0 6d c0 af 1a 7b 07 2e 74 75",
        ),
        (
            SESSION_KEY,
            "aa 01 00 00 81 00 02 01 00 00 00 00 00 00 00 00 00 00 00 29",
            "c5 99 fd 16 d0 72 d6 d3 3a 16 1c 2e 18 dc 3a 9f 07 2e 74 f7",
        ),
    ],
)
def test_captured_govee_v1_vectors(
    key: bytes, plaintext: str, encrypted: str
) -> None:
    plaintext_frame = bytes.fromhex(plaintext)
    encrypted_frame = bytes.fromhex(encrypted)

    assert encrypt_frame(plaintext_frame, key) == encrypted_frame
    assert decrypt_frame(encrypted_frame, key) == plaintext_frame


def test_captured_session_response_exposes_negotiated_key() -> None:
    response = decrypt_frame(
        bytes.fromhex(
            "d2 d6 11 36 42 d9 eb 3a 58 50 b3 2f 74 a8 82 d5 a8 03 5c 7d"
        ),
        COMMUNICATION_KEY,
    )

    assert parse_session_key(response) == SESSION_KEY


@pytest.mark.parametrize("command", [0x01, 0x02])
def test_identify_handshake_frame_accepts_valid_commands(command: int) -> None:
    plaintext = build_frame(bytes((0xE7, command)) + bytes(range(17)))

    wire_frame = encrypt_frame(plaintext, COMMUNICATION_KEY)

    assert identify_handshake_frame(wire_frame) == command


@pytest.mark.parametrize(
    "wire_frame",
    [
        b"short",
        encrypt_frame(build_frame(b"\xaa\x01"), COMMUNICATION_KEY),
        encrypt_frame(build_frame(b"\xe7\x03"), COMMUNICATION_KEY),
        encrypt_frame(build_frame(b"\xaa\x01"), SESSION_KEY),
    ],
)
def test_identify_handshake_frame_rejects_other_frames(wire_frame: bytes) -> None:
    assert identify_handshake_frame(wire_frame) is None


def test_handshake_request_uses_supplied_random_payload() -> None:
    payload = bytes(range(17))

    assert build_handshake_request(0x01, random_payload=payload) == build_frame(
        b"\xe7\x01" + payload
    )


def test_handshake_confirmation_requires_exact_e7_02_echo() -> None:
    request = build_frame(bytes.fromhex("e7 02") + bytes(range(17)))

    validate_handshake_confirmation(request, request)

    with pytest.raises(ProtocolError, match="Invalid e7 02"):
        validate_handshake_confirmation(
            build_frame(bytes.fromhex("e7 02") + bytes(reversed(range(17)))), request
        )


@pytest.mark.parametrize("command", [0x00, 0x03, 0xFF])
def test_handshake_request_rejects_unknown_command(command: int) -> None:
    with pytest.raises(ProtocolError, match="Unsupported handshake command"):
        build_handshake_request(command)


def test_crypto_rejects_invalid_key_and_frames() -> None:
    frame = build_frame(b"\xaa\x01")

    with pytest.raises(ProtocolError, match="16-byte encryption key"):
        encrypt_frame(frame, b"short")
    with pytest.raises(ProtocolError, match="Expected 20 bytes"):
        decrypt_frame(b"short", COMMUNICATION_KEY)
    with pytest.raises(ProtocolError, match="Invalid checksum"):
        encrypt_frame(bytes(19) + b"\x01", COMMUNICATION_KEY)


def test_session_key_parser_rejects_other_frames() -> None:
    with pytest.raises(ProtocolError, match="Not an e7 01"):
        parse_session_key(build_frame(b"\xe7\x02"))
