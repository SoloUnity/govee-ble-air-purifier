"""Protocol helpers for Govee H7124-style BLE air purifiers."""

from __future__ import annotations

from .models import GoveeAirPurifierState

FRAME_LENGTH = 20
MAX_PM25_UG_M3 = 999


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


POWER_OFF_COMMAND = build_frame(bytes.fromhex("33 01 00"))
POWER_ON_COMMAND = build_frame(bytes.fromhex("33 01 01"))
STATE_QUERY_COMMAND = build_frame(bytes.fromhex("aa 01"))
STATUS_QUERY_COMMAND = build_frame(bytes.fromhex("aa 19"))

FAN_MODE_COMMANDS: dict[str, bytes] = {
    "Low": bytes.fromhex("3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f"),
    "Medium": bytes.fromhex("3a 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3c"),
    "High": bytes.fromhex("3a 05 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3d"),
    "Sleep": bytes.fromhex("3a 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3a"),
    "Auto": bytes.fromhex("3a 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 28"),
    "Turbo": bytes.fromhex("3a 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 38"),
}
FAN_MODE_LABELS = list(FAN_MODE_COMMANDS)
MODE_PUSH_LABELS: dict[int, str] = {
    0x03: "Auto",
    0x05: "Sleep",
    0x07: "Turbo",
}


def is_power_state_response(frame: bytes) -> bool:
    """Return True if frame looks like an aa01 state response."""

    return len(frame) == FRAME_LENGTH and frame[0] == 0xAA and frame[1] == 0x01


def is_status_response(frame: bytes) -> bool:
    """Return True if frame looks like an aa19 status response."""

    return len(frame) == FRAME_LENGTH and frame[0] == 0xAA and frame[1] == 0x19


def is_mode_push(frame: bytes) -> bool:
    """Return True if frame looks like an ee05 mode push."""

    return len(frame) == FRAME_LENGTH and frame[0] == 0xEE and frame[1] == 0x05


def is_command_echo(frame: bytes, command: bytes) -> bool:
    """Return True when a notification exactly echoes a command frame."""

    if frame != command:
        return False
    try:
        validate_frame(frame)
    except ProtocolError:
        return False
    return True


def is_power_confirmation(frame: bytes, is_on: bool) -> bool:
    """Return True when an aa01 frame confirms the requested power state."""

    try:
        return decode_power_state(frame) is is_on
    except ProtocolError:
        return False


def decode_mode_push(frame: bytes) -> str:
    """Decode Sleep, Auto, or Turbo from an ee05 mode push."""

    validate_frame(frame)
    if not is_mode_push(frame):
        raise ProtocolError("Not an ee05 mode push")
    try:
        return MODE_PUSH_LABELS[frame[2]]
    except KeyError as err:
        raise ProtocolError(f"Unknown mode push byte 0x{frame[2]:02x}") from err


def is_fan_mode_confirmation(frame: bytes, mode: str, command: bytes) -> bool:
    """Return True when a frame confirms a fan mode command."""

    if is_command_echo(frame, command):
        return True
    try:
        return decode_mode_push(frame) == mode
    except ProtocolError:
        return False


def decode_power_state(frame: bytes) -> bool:
    """Decode power state from an aa01 response."""

    validate_frame(frame)
    if not is_power_state_response(frame):
        raise ProtocolError("Not an aa01 power state response")
    if frame[2] not in (0x00, 0x01):
        raise ProtocolError(f"Unknown power byte 0x{frame[2]:02x}")
    return frame[2] == 0x01


def decode_status(frame: bytes) -> GoveeAirPurifierState:
    """Decode PM2.5 and filter-life values from an aa19 response."""

    validate_frame(frame)
    if not is_status_response(frame):
        raise ProtocolError("Not an aa19 status response")
    raw_pm25 = (frame[3] << 8) | frame[4]
    return GoveeAirPurifierState(
        pm25=raw_pm25 if raw_pm25 <= MAX_PM25_UG_M3 else None,
        filter_life=frame[7],
    )


def normalize_ble_name(name: str | None) -> str | None:
    """Return a stable human/unique-id suffix for GVH7124 BLE names."""

    if not name or not name.startswith("GVH7124"):
        return None
    suffix = name.removeprefix("GVH7124")
    return f"H7124-{suffix}" if suffix else "H7124"
