"""Home Assistant-independent Govee V1 encrypted-session frame helpers."""

from __future__ import annotations

import secrets
from typing import cast

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .framing import FRAME_LENGTH, ProtocolError, build_frame, validate_frame

COMMUNICATION_KEY = b"MakingLifeSmarte"
KEY_LENGTH = 16
HANDSHAKE_PAYLOAD_LENGTH = 17
SESSION_KEY_START = 2
SESSION_KEY_END = SESSION_KEY_START + KEY_LENGTH


def _validate_key(key: bytes) -> None:
    """Validate one Govee V1 AES/RC4 key."""

    if not isinstance(key, bytes) or len(key) != KEY_LENGTH:
        raise ProtocolError(f"Expected a {KEY_LENGTH}-byte encryption key")


def _rc4_xor(data: bytes, key: bytes) -> bytes:
    """Apply the per-frame RC4-compatible Govee tail transform."""

    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]

    i = 0
    j = 0
    transformed = bytearray()
    for value in data:
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        transformed.append(value ^ state[(state[i] + state[j]) & 0xFF])
    return bytes(transformed)


def encrypt_frame(frame: bytes, key: bytes) -> bytes:
    """Encrypt one checksum-valid 20-byte application frame."""

    _validate_key(key)
    validate_frame(frame)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    encrypted_block = encryptor.update(frame[:KEY_LENGTH]) + encryptor.finalize()
    return cast(bytes, encrypted_block + _rc4_xor(frame[KEY_LENGTH:], key))


def decrypt_frame(frame: bytes, key: bytes) -> bytes:
    """Decrypt and validate one 20-byte Govee V1 wire frame."""

    _validate_key(key)
    if len(frame) != FRAME_LENGTH:
        raise ProtocolError(f"Expected {FRAME_LENGTH} bytes, got {len(frame)}")
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    decrypted_block = decryptor.update(frame[:KEY_LENGTH]) + decryptor.finalize()
    plaintext = decrypted_block + _rc4_xor(frame[KEY_LENGTH:], key)
    validate_frame(plaintext)
    return cast(bytes, plaintext)


def identify_handshake_frame(frame: bytes) -> int | None:
    """Identify a checksum-valid communication-key handshake wire frame."""

    try:
        plaintext = decrypt_frame(frame, COMMUNICATION_KEY)
    except ProtocolError:
        return None
    if plaintext[:2] in (b"\xe7\x01", b"\xe7\x02"):
        return plaintext[1]
    return None


def build_handshake_request(
    command: int, *, random_payload: bytes | None = None
) -> bytes:
    """Build a checksum-valid e7 handshake request with random payload bytes."""

    if command not in (0x01, 0x02):
        raise ProtocolError(f"Unsupported handshake command 0x{command:02x}")
    if random_payload is None:
        random_payload = secrets.token_bytes(HANDSHAKE_PAYLOAD_LENGTH)
    if len(random_payload) != HANDSHAKE_PAYLOAD_LENGTH:
        raise ProtocolError(
            f"Expected {HANDSHAKE_PAYLOAD_LENGTH} handshake payload bytes"
        )
    return build_frame(bytes((0xE7, command)) + random_payload)


def parse_session_key(frame: bytes) -> bytes:
    """Extract the negotiated key from a plaintext e7 01 response."""

    validate_frame(frame)
    if frame[:2] != b"\xe7\x01":
        raise ProtocolError("Not an e7 01 session-key response")
    return frame[SESSION_KEY_START:SESSION_KEY_END]


def validate_handshake_confirmation(frame: bytes, request: bytes) -> None:
    """Validate that the device echoed the plaintext e7 02 request."""

    validate_frame(frame)
    if request[:2] != b"\xe7\x02" or frame != request:
        raise ProtocolError("Invalid e7 02 handshake confirmation")
