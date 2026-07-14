"""Model profiles for supported Govee BLE air purifiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from uuid import UUID

from .models import GoveeAirPurifierState
from .protocol import (
    FAN_MODE_COMMANDS,
    POWER_OFF_COMMAND,
    POWER_ON_COMMAND,
    STATE_QUERY_COMMAND,
    STATUS_QUERY_COMMAND,
    decode_power_state,
    decode_status,
    is_power_state_response,
    is_status_response,
)


@dataclass(frozen=True)
class ModelProfile:
    """BLE protocol and capabilities for one purifier model family."""

    key: str
    model: str
    display_name: str
    local_name_prefixes: tuple[str, ...]
    service_uuid: str
    notify_char_uuid: str
    write_char_uuid: str
    power_off_command: bytes
    power_on_command: bytes
    state_query_command: bytes
    status_query_command: bytes
    fan_mode_commands: dict[str, bytes]
    is_power_state_response: Callable[[bytes], bool]
    is_status_response: Callable[[bytes], bool]
    decode_power_state: Callable[[bytes], bool]
    decode_status: Callable[[bytes], GoveeAirPurifierState]

    def matches_local_name(self, name: str | None) -> bool:
        """Return true if a BLE local name belongs to this model profile."""

        return bool(name) and name.startswith(self.local_name_prefixes)


H7124_PROFILE = ModelProfile(
    key="h7124",
    model="H7124",
    display_name="Govee H7124 Air Purifier",
    local_name_prefixes=("GVH7124",),
    service_uuid="00010203-0405-0607-0809-0a0b0c0d1910",
    notify_char_uuid="00010203-0405-0607-0809-0a0b0c0d2b10",
    write_char_uuid="00010203-0405-0607-0809-0a0b0c0d2b11",
    power_off_command=POWER_OFF_COMMAND,
    power_on_command=POWER_ON_COMMAND,
    state_query_command=STATE_QUERY_COMMAND,
    status_query_command=STATUS_QUERY_COMMAND,
    fan_mode_commands=FAN_MODE_COMMANDS,
    is_power_state_response=is_power_state_response,
    is_status_response=is_status_response,
    decode_power_state=decode_power_state,
    decode_status=decode_status,
)

PROFILES: tuple[ModelProfile, ...] = (H7124_PROFILE,)
PROFILES_BY_KEY = {profile.key: profile for profile in PROFILES}


def match_profile(name: str | None) -> ModelProfile | None:
    """Return the registered profile matching a BLE local name."""

    return next(
        (profile for profile in PROFILES if profile.matches_local_name(name)), None
    )


def get_profile(key: str | None) -> ModelProfile:
    """Return a profile by key, defaulting to H7124 for existing entries."""

    if key is None:
        return H7124_PROFILE
    try:
        return PROFILES_BY_KEY[key]
    except KeyError as err:
        raise ValueError(f"Unsupported purifier profile: {key}") from err


def fan_mode_labels(profile: ModelProfile) -> list[str]:
    """Return fan mode labels in profile command order."""

    return list(profile.fan_mode_commands)


def normalize_ble_address(address: str) -> str:
    """Normalize a BLE address for stable config-entry unique IDs."""

    return re.sub(r"[^0-9a-f]", "", address.lower())


def canonicalize_ble_address(address: str) -> str:
    """Validate and canonicalize a platform BLE address."""

    value = address.strip()
    if re.fullmatch(
        r"(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}|"
        r"(?:[0-9A-Fa-f]{2}-){5}[0-9A-Fa-f]{2}",
        value,
    ):
        return value.replace("-", ":").upper()
    if re.fullmatch(
        r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
        r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}",
        value,
    ):
        return str(UUID(value)).upper()
    raise ValueError("Invalid BLE address")
