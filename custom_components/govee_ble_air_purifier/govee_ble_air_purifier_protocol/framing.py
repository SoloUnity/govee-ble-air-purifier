"""Home Assistant-independent Govee BLE frame helpers."""

from __future__ import annotations

FRAME_LENGTH = 20


class ProtocolError(ValueError):
    """Raised when a BLE frame is malformed or unexpected."""


def _xor_checksum(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum


def build_frame(prefix: bytes) -> bytes:
    """Build a 20-byte frame from bytes 0..n and append the XOR checksum."""

    if len(prefix) > FRAME_LENGTH - 1:
        raise ProtocolError("Frame payload must fit in the first 19 bytes")
    body = prefix.ljust(FRAME_LENGTH - 1, b"\x00")
    return body + bytes([_xor_checksum(body)])


def validate_frame(frame: bytes) -> None:
    """Validate frame length and XOR checksum."""

    if len(frame) != FRAME_LENGTH:
        raise ProtocolError(f"Expected {FRAME_LENGTH} bytes, got {len(frame)}")
    expected = _xor_checksum(frame[:-1])
    if frame[-1] != expected:
        raise ProtocolError(
            f"Invalid checksum 0x{frame[-1]:02x}; expected 0x{expected:02x}"
        )
