"""Model profiles for supported Govee BLE air purifiers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any
from uuid import UUID

from .bluetooth.framing import ProtocolError, validate_frame
from .models import DecodedStatus
from .protocol import (
    decode_power_state,
    decode_status,
    is_power_state_response,
    is_status_response,
)

PROFILE_SCHEMA_VERSION = 1
PROFILE_DIRECTORY = Path(__file__).with_name("model_profiles")
DEFAULT_PROFILE_KEY = "default"
H7124_PROFILE_KEY = "h7124"

_BLE_MODEL_PATTERN = re.compile(r"(H712[0-9A-Z])", re.IGNORECASE | re.ASCII)
_PROFILE_KEY_PATTERN = re.compile(r"h712[0-9a-z]\Z")
_TOP_LEVEL_KEYS = {"schema_version", "encryption", "gatt", "commands"}
_GATT_KEYS = {"service_uuid", "notify_char_uuid", "write_char_uuid"}
_COMMAND_KEYS = {
    "power_off",
    "power_on",
    "state_query",
    "status_query",
    "fan_modes",
}
_CUSTOM_AUTO_REQUIRED_FAN_MODES = frozenset(
    {"Sleep", "Low", "Medium", "High", "Turbo", "Auto"}
)


class _DuplicateProfileKeyError(ValueError):
    """Raised when a profile JSON object repeats a key."""


class EncryptionMode(StrEnum):
    """Supported model-profile transport encryption modes."""

    NONE = "none"
    GOVEE_V1 = "govee_v1"


@dataclass(frozen=True)
class ModelProfile:
    """BLE protocol and capabilities for one purifier model."""

    key: str
    model: str
    display_name: str
    local_name_prefixes: tuple[str, ...]
    encryption: EncryptionMode
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
    decode_status: Callable[[bytes], DecodedStatus]

    def matches_local_name(self, name: str | None) -> bool:
        """Return true if a BLE local name belongs to this model profile."""

        return model_from_ble_name(name) == self.model

    @property
    def supports_custom_auto(self) -> bool:
        """Return whether all modes required by Custom Auto are available."""

        return _CUSTOM_AUTO_REQUIRED_FAN_MODES <= self.fan_mode_commands.keys()


@dataclass(frozen=True)
class _ProfileDefinition:
    encryption: EncryptionMode
    service_uuid: str
    notify_char_uuid: str
    write_char_uuid: str
    power_off_command: bytes
    power_on_command: bytes
    state_query_command: bytes
    status_query_command: bytes
    fan_mode_commands: dict[str, bytes]


def _require_object(
    value: Any, expected_keys: set[str], *, source: str
) -> dict[str, Any]:
    """Validate one object in a bundled profile definition."""

    if not isinstance(value, dict):
        raise ValueError(f"{source} must be a JSON object")
    actual_keys = set(value)
    if actual_keys != expected_keys:
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if unknown:
            details.append(f"unknown {', '.join(unknown)}")
        raise ValueError(f"{source} has invalid keys: {'; '.join(details)}")
    return value


def _parse_uuid(value: Any, *, source: str) -> str:
    """Return one canonical UUID from a profile definition."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a UUID string")
    try:
        return str(UUID(value))
    except ValueError as err:
        raise ValueError(f"{source} is not a valid UUID") from err


def _parse_frame(value: Any, *, source: str) -> bytes:
    """Parse and validate one complete BLE command frame."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a hexadecimal string")
    try:
        frame = bytes.fromhex(value)
        validate_frame(frame)
    except (ValueError, ProtocolError) as err:
        raise ValueError(f"{source} is not a valid Govee BLE frame: {err}") from err
    return frame


def _parse_encryption(value: Any, *, source: str) -> EncryptionMode:
    """Return one supported profile encryption mode."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a string")
    try:
        return EncryptionMode(value)
    except ValueError as err:
        supported = ", ".join(mode.value for mode in EncryptionMode)
        raise ValueError(f"{source} must be one of {supported}") from err


def _parse_profile_definition(data: Any, *, source: str) -> _ProfileDefinition:
    """Validate and normalize one complete profile definition."""

    profile = _require_object(data, _TOP_LEVEL_KEYS, source=source)
    if (
        type(profile["schema_version"]) is not int
        or profile["schema_version"] != PROFILE_SCHEMA_VERSION
    ):
        raise ValueError(
            f"{source}.schema_version must be {PROFILE_SCHEMA_VERSION}"
        )
    gatt = _require_object(profile["gatt"], _GATT_KEYS, source=f"{source}.gatt")
    commands = _require_object(
        profile["commands"], _COMMAND_KEYS, source=f"{source}.commands"
    )
    fan_modes = commands["fan_modes"]
    if not isinstance(fan_modes, dict) or not fan_modes:
        raise ValueError(f"{source}.commands.fan_modes must be a non-empty object")
    parsed_fan_modes: dict[str, bytes] = {}
    for mode, frame in fan_modes.items():
        if not isinstance(mode, str) or not mode.strip() or mode != mode.strip():
            raise ValueError(
                f"{source}.commands.fan_modes contains an invalid mode name"
            )
        parsed_fan_modes[mode] = _parse_frame(
            frame, source=f"{source}.commands.fan_modes.{mode}"
        )

    return _ProfileDefinition(
        encryption=_parse_encryption(
            profile["encryption"], source=f"{source}.encryption"
        ),
        service_uuid=_parse_uuid(
            gatt["service_uuid"], source=f"{source}.gatt.service_uuid"
        ),
        notify_char_uuid=_parse_uuid(
            gatt["notify_char_uuid"], source=f"{source}.gatt.notify_char_uuid"
        ),
        write_char_uuid=_parse_uuid(
            gatt["write_char_uuid"], source=f"{source}.gatt.write_char_uuid"
        ),
        power_off_command=_parse_frame(
            commands["power_off"], source=f"{source}.commands.power_off"
        ),
        power_on_command=_parse_frame(
            commands["power_on"], source=f"{source}.commands.power_on"
        ),
        state_query_command=_parse_frame(
            commands["state_query"], source=f"{source}.commands.state_query"
        ),
        status_query_command=_parse_frame(
            commands["status_query"], source=f"{source}.commands.status_query"
        ),
        fan_mode_commands=parsed_fan_modes,
    )


def _reject_duplicate_profile_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Build a JSON object while rejecting silently overwritten keys."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateProfileKeyError(f"duplicate key {key}")
        result[key] = value
    return result


def _decode_profile_json(raw: str, *, source: str) -> Any:
    """Decode profile JSON with duplicate-key protection."""

    try:
        return json.loads(raw, object_pairs_hook=_reject_duplicate_profile_keys)
    except (json.JSONDecodeError, _DuplicateProfileKeyError) as err:
        raise ValueError(f"Unable to load model profile {source}: {err}") from err


def _load_profile_definitions(
    profile_directory: Path = PROFILE_DIRECTORY,
) -> dict[str, _ProfileDefinition]:
    """Load bundled definitions once while the integration module is imported."""

    definitions: dict[str, _ProfileDefinition] = {}
    for path in sorted(profile_directory.glob("*.json")):
        key = path.stem
        if key != DEFAULT_PROFILE_KEY and _PROFILE_KEY_PATTERN.fullmatch(key) is None:
            raise ValueError(f"Invalid model profile filename: {path.name}")
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as err:
            raise ValueError(f"Unable to load model profile {path.name}: {err}") from err
        data = _decode_profile_json(raw, source=path.name)
        definitions[key] = _parse_profile_definition(data, source=path.name)
    if DEFAULT_PROFILE_KEY not in definitions:
        raise ValueError("Missing model_profiles/default.json")
    if H7124_PROFILE_KEY not in definitions:
        raise ValueError("Missing model_profiles/h7124.json")
    return definitions


_PROFILE_DEFINITIONS = _load_profile_definitions()


def model_from_ble_name(name: str | None) -> str | None:
    """Extract an H712-family model from a Govee BLE local name."""

    if not name or (match := _BLE_MODEL_PATTERN.search(name)) is None:
        return None
    return match.group(1).upper()


def normalize_ble_name(name: str | None) -> str | None:
    """Return a stable human-readable identifier for an H712 BLE name."""

    if not name or (match := _BLE_MODEL_PATTERN.search(name)) is None:
        return None
    model = match.group(1).upper()
    suffix = name[match.end() :].lstrip(" _-")
    return f"{model}-{suffix}" if suffix else model


def match_profile(name: str | None) -> ModelProfile | None:
    """Return the registered profile matching a BLE local name."""

    model = model_from_ble_name(name)
    return get_profile(model.lower()) if model is not None else None


def get_profile(key: str | None) -> ModelProfile:
    """Resolve an exact model definition or the H7124-compatible fallback."""

    resolved_key = H7124_PROFILE_KEY if key is None else key
    if not isinstance(resolved_key, str) or not _PROFILE_KEY_PATTERN.fullmatch(
        resolved_key
    ):
        raise ValueError(f"Unsupported purifier profile: {key}")
    return _get_profile(resolved_key)


@lru_cache
def _get_profile(resolved_key: str) -> ModelProfile:
    """Build and cache one validated H712-family profile."""

    return _build_profile(resolved_key, _PROFILE_DEFINITIONS)


def _build_profile(
    resolved_key: str, definitions: dict[str, _ProfileDefinition]
) -> ModelProfile:
    """Build one profile from an exact definition or the default fallback."""

    model = resolved_key.upper()
    definition = definitions.get(resolved_key, definitions[DEFAULT_PROFILE_KEY])
    return ModelProfile(
        key=resolved_key,
        model=model,
        display_name=f"Govee {model} Air Purifier",
        local_name_prefixes=(f"GV{model}",),
        encryption=definition.encryption,
        service_uuid=definition.service_uuid,
        notify_char_uuid=definition.notify_char_uuid,
        write_char_uuid=definition.write_char_uuid,
        power_off_command=definition.power_off_command,
        power_on_command=definition.power_on_command,
        state_query_command=definition.state_query_command,
        status_query_command=definition.status_query_command,
        fan_mode_commands=dict(definition.fan_mode_commands),
        is_power_state_response=is_power_state_response,
        is_status_response=is_status_response,
        decode_power_state=decode_power_state,
        decode_status=decode_status,
    )


H7124_PROFILE = get_profile(H7124_PROFILE_KEY)
PROFILES: tuple[ModelProfile, ...] = tuple(
    get_profile(key) for key in _PROFILE_DEFINITIONS if key != DEFAULT_PROFILE_KEY
)
PROFILES_BY_KEY = {profile.key: profile for profile in PROFILES}


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
