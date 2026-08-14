"""Shared response helpers for H712-family Govee BLE air purifiers."""

from __future__ import annotations

from .bluetooth.framing import (
    FRAME_LENGTH,
    ProtocolError,
    validate_frame,
)
from .models import DecodedStatus, NightLightState

MAX_PM25_UG_M3 = 999


def is_power_state_response(frame: bytes) -> bool:
    """Return True if frame looks like an aa01 state response."""

    return (
        len(frame) == FRAME_LENGTH
        and frame[0] == 0xAA
        and frame[1] == 0x01
        and frame[4] == 0x81
    )


def is_status_response(frame: bytes) -> bool:
    """Return True if frame looks like an aa19 status response."""

    return (
        len(frame) == FRAME_LENGTH
        and frame[0] == 0xAA
        and frame[1] == 0x19
        and frame[2] == 0x81
    )


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


def is_night_light_power_brightness_response(frame: bytes) -> bool:
    """Return whether a frame reports night-light power and brightness."""

    return (
        len(frame) == FRAME_LENGTH
        and frame[0] in (0xAA, 0x3A)
        and frame[1] == 0x1B
        and frame[2] == 0x01
        and frame[3] in (0x00, 0x01)
        and 1 <= frame[4] <= 100
        and not any(frame[5:19])
    )


def is_night_light_rgb_state_response(frame: bytes) -> bool:
    """Return whether a frame answers a night-light color-state query."""

    return (
        len(frame) == FRAME_LENGTH
        and frame[0] == 0xAA
        and frame[1] == 0x1B
        and frame[2] == 0x05
        and (
            (frame[3] == 0x0D and not any(frame[7:19]))
            or (frame[3] == 0xFC and not any(frame[4:19]))
        )
    )


def decode_night_light_power_brightness(frame: bytes) -> NightLightState:
    """Decode a night-light power and brightness report."""

    validate_frame(frame)
    if not is_night_light_power_brightness_response(frame):
        raise ProtocolError("Not a night-light power/brightness response")
    return NightLightState(
        is_on=frame[3] == 0x01,
        brightness_percent=frame[4],
    )


def decode_night_light_rgb_state(
    frame: bytes,
) -> tuple[int, int, int] | None:
    """Decode RGB state, or return None for an unknown color discriminator."""

    validate_frame(frame)
    if not is_night_light_rgb_state_response(frame):
        raise ProtocolError("Not a night-light RGB state response")
    if frame[3] == 0xFC:
        return None
    return (frame[4], frame[5], frame[6])


def is_night_light_power_confirmation(frame: bytes, is_on: bool) -> bool:
    """Return whether a night-light report confirms requested power."""

    try:
        return decode_night_light_power_brightness(frame).is_on is is_on
    except ProtocolError:
        return False


def is_night_light_brightness_confirmation(
    frame: bytes, brightness_percent: int
) -> bool:
    """Return whether a night-light report confirms requested brightness."""

    try:
        state = decode_night_light_power_brightness(frame)
    except ProtocolError:
        return False
    return state.is_on is True and state.brightness_percent == brightness_percent


def is_power_confirmation(frame: bytes, is_on: bool) -> bool:
    """Return True when an aa01 frame confirms the requested power state."""

    try:
        return decode_power_state(frame) is is_on
    except ProtocolError:
        return False


def decode_mode_push(frame: bytes, fan_mode_commands: dict[str, bytes]) -> str:
    """Decode an ee05 push against one model's configured fan commands."""

    validate_frame(frame)
    if not is_mode_push(frame):
        raise ProtocolError("Not an ee05 mode push")
    for mode, command in fan_mode_commands.items():
        if frame[2:19] == command[2:19]:
            return mode
    raise ProtocolError("Mode push does not match this purifier profile")


def decode_night_light_power_brightness_push(frame: bytes) -> NightLightState:
    """Decode an unsolicited ee1b01 night-light power/brightness update."""

    validate_frame(frame)
    if not (
        len(frame) == FRAME_LENGTH
        and frame[0] == 0xEE
        and frame[1] == 0x1B
        and frame[2] == 0x01
        and frame[3] in (0x00, 0x01)
        and 1 <= frame[4] <= 100
        and not any(frame[5:19])
    ):
        raise ProtocolError("Not an ee1b01 night-light push")
    return NightLightState(
        is_on=frame[3] == 0x01,
        brightness_percent=frame[4],
    )


def is_fan_mode_confirmation(frame: bytes, mode: str, command: bytes) -> bool:
    """Return True when a frame confirms a fan mode command."""

    if is_command_echo(frame, command):
        return True
    try:
        return decode_mode_push(frame, {mode: command}) == mode
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


def decode_status(frame: bytes) -> DecodedStatus:
    """Decode PM2.5 and filter-life values from an aa19 response."""

    validate_frame(frame)
    if not is_status_response(frame):
        raise ProtocolError("Not an aa19 status response")
    raw_pm25 = (frame[3] << 8) | frame[4]
    return DecodedStatus(
        pm25=raw_pm25 if raw_pm25 <= MAX_PM25_UG_M3 else None,
        filter_life=frame[7],
    )
