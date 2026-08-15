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

from .framing import ProtocolError, build_frame, validate_frame
from .models import DecodedStatus
from .protocol import (
    decode_power_state,
    decode_status,
    is_power_state_response,
    is_status_response,
)

MIN_POLLING_INTERVAL_SECONDS = 3
MAX_POLLING_INTERVAL_SECONDS = 300
PROFILE_SCHEMA_VERSION = 5
PROFILE_DIRECTORY = Path(__file__).with_name("model_profiles")
MODEL_PROFILE_SCHEMA_PATH = (
    Path(__file__).with_name("schemas") / "model_profile_v5.schema.json"
)
DEFAULT_PROFILE_KEY = "default"
H7124_PROFILE_KEY = "h7124"

_BLE_MODEL_PATTERN = re.compile(r"(H712[0-9A-Z])", re.IGNORECASE | re.ASCII)
_PROFILE_KEY_PATTERN = re.compile(r"h712[0-9a-z]\Z")
_TOP_LEVEL_KEYS = {
    "schema_version",
    "support_status",
    "polling_interval_seconds",
    "encryption",
    "gatt",
    "commands",
}
_TOP_LEVEL_OPTIONAL_KEYS = {"custom_auto", "night_light", "push_notifications"}
_GATT_KEYS = {"service_uuid", "notify_char_uuid", "write_char_uuid"}
_CUSTOM_AUTO_KEYS = {"thresholds"}
_COMMAND_KEYS = {
    "power_off",
    "power_on",
    "state_query",
    "status_query",
    "fan_modes",
}
_NIGHT_LIGHT_KEYS = {
    "polling",
    "power_off",
    "power_on",
    "power_brightness_query",
    "brightness_template",
    "rgb_template",
    "rgb_state_query",
}
_NIGHT_LIGHT_POLLING_KEYS = {
    "cadence",
    "interval_seconds",
    "timeout_seconds",
    "request_order",
    "max_backoff_seconds",
}
_PUSH_NOTIFICATION_KEYS = {
    "power_state",
    "fan_mode",
    "night_light_power_brightness",
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


class ModelSupportStatus(StrEnum):
    """Evidence level for one model profile."""

    VERIFIED = "verified"
    READ_VERIFIED = "read_verified"
    EXPERIMENTAL = "experimental"
    FALLBACK = "fallback"


class NightLightPollingCadence(StrEnum):
    """Supported night-light reconciliation schedules."""

    EVERY_POLL = "every_poll"
    PERIODIC = "periodic"


class NightLightPollingRequestOrder(StrEnum):
    """Supported night-light query dispatch strategies."""

    PIPELINED = "pipelined"
    SEQUENTIAL = "sequential"


@dataclass(frozen=True)
class _FrameTemplate:
    """Validated variable application-frame template."""

    tokens: tuple[int | str, ...]
    placeholders: frozenset[str]

    def render(self, **values: int) -> bytes:
        """Render one checksum-valid frame from validated byte values."""

        if set(values) != self.placeholders:
            raise ValueError("Frame template values do not match its placeholders")
        rendered: list[int] = []
        for token in self.tokens:
            value = values[token] if isinstance(token, str) else token
            if type(value) is not int or not 0 <= value <= 0xFF:
                raise ValueError(f"Frame template value {token} must be a byte")
            rendered.append(value)
        return build_frame(bytes(rendered))


@dataclass(frozen=True)
class NightLightPollingProfile:
    """Profile-defined night-light reconciliation behavior."""

    cadence: NightLightPollingCadence
    interval_seconds: int
    timeout_seconds: int
    request_order: NightLightPollingRequestOrder
    max_backoff_seconds: int


@dataclass(frozen=True)
class NightLightProfile:
    """Profile-defined commands for an optional purifier night light."""

    polling: NightLightPollingProfile
    power_off_command: bytes
    power_on_command: bytes
    power_brightness_query_command: bytes
    brightness_template: _FrameTemplate
    rgb_template: _FrameTemplate
    rgb_state_query_command: bytes

    def build_brightness_command(self, brightness_percent: int) -> bytes:
        """Build a brightness command for one device percentage."""

        if type(brightness_percent) is not int or not 1 <= brightness_percent <= 100:
            raise ValueError("Night-light brightness must be from 1 to 100")
        return self.brightness_template.render(brightness=brightness_percent)

    def build_rgb_command(self, rgb_color: tuple[int, int, int]) -> bytes:
        """Build an RGB command for one three-channel color."""

        if not isinstance(rgb_color, tuple) or len(rgb_color) != 3:
            raise ValueError("Night-light RGB color must contain three channels")
        red, green, blue = rgb_color
        for channel in rgb_color:
            if type(channel) is not int or not 0 <= channel <= 0xFF:
                raise ValueError("Night-light RGB channels must be bytes")
        return self.rgb_template.render(red=red, green=green, blue=blue)


@dataclass(frozen=True)
class PushNotificationProfile:
    """Profile-enabled unsolicited state notifications."""

    power_state: bool
    fan_mode: bool
    night_light_power_brightness: bool

    @property
    def enabled(self) -> bool:
        """Return whether any persistent push decoder is enabled."""

        return self.power_state or self.fan_mode or self.night_light_power_brightness


@dataclass(frozen=True)
class ModelProfile:
    """BLE protocol and capabilities for one purifier model."""

    key: str
    model: str
    display_name: str
    local_name_prefixes: tuple[str, ...]
    support_status: ModelSupportStatus
    polling_interval_seconds: int
    encryption: EncryptionMode
    service_uuid: str
    notify_char_uuid: str
    write_char_uuid: str
    power_off_command: bytes
    power_on_command: bytes
    state_query_command: bytes
    status_query_command: bytes
    fan_mode_commands: dict[str, bytes]
    custom_auto_thresholds: tuple[int, int, int, int] | None
    night_light: NightLightProfile | None
    push_notifications: PushNotificationProfile | None
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

        return (
            self.custom_auto_thresholds is not None
            and _CUSTOM_AUTO_REQUIRED_FAN_MODES <= self.fan_mode_commands.keys()
        )

    @property
    def requires_support_acknowledgement(self) -> bool:
        """Return whether setup should disclose incomplete model verification."""

        return self.support_status is not ModelSupportStatus.VERIFIED


@dataclass(frozen=True)
class _ProfileDefinition:
    support_status: ModelSupportStatus
    polling_interval_seconds: int
    encryption: EncryptionMode
    service_uuid: str
    notify_char_uuid: str
    write_char_uuid: str
    power_off_command: bytes
    power_on_command: bytes
    state_query_command: bytes
    status_query_command: bytes
    fan_mode_commands: dict[str, bytes]
    custom_auto_thresholds: tuple[int, int, int, int] | None
    night_light: NightLightProfile | None
    push_notifications: PushNotificationProfile | None


def _require_object(
    value: Any,
    expected_keys: set[str],
    *,
    source: str,
    optional_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Validate one object in a bundled profile definition."""

    if not isinstance(value, dict):
        raise ValueError(f"{source} must be a JSON object")
    optional_keys = optional_keys or set()
    actual_keys = set(value)
    if (
        not expected_keys <= actual_keys
        or not actual_keys <= expected_keys | optional_keys
    ):
        missing = sorted(expected_keys - actual_keys)
        unknown = sorted(actual_keys - expected_keys - optional_keys)
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


def _parse_frame_template(
    value: Any, *, placeholders: set[str], source: str
) -> _FrameTemplate:
    """Parse a variable command prefix rendered through the frame builder."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a frame template string")
    tokens: list[int | str] = []
    seen_placeholders: list[str] = []
    for raw_token in value.split():
        if raw_token.startswith("{") or raw_token.endswith("}"):
            if not re.fullmatch(r"\{[a-z_]+\}", raw_token):
                raise ValueError(f"{source} contains invalid placeholder {raw_token}")
            placeholder = raw_token[1:-1]
            if placeholder not in placeholders:
                raise ValueError(f"{source} contains unknown placeholder {raw_token}")
            seen_placeholders.append(placeholder)
            tokens.append(placeholder)
            continue
        if re.fullmatch(r"[0-9A-Fa-f]{2}", raw_token) is None:
            raise ValueError(f"{source} contains invalid byte {raw_token}")
        tokens.append(int(raw_token, 16))
    if len(tokens) > 19:
        raise ValueError(f"{source} must fit in the first 19 frame bytes")
    if sorted(seen_placeholders) != sorted(placeholders):
        raise ValueError(
            f"{source} must contain each placeholder exactly once: "
            + ", ".join(f"{{{name}}}" for name in sorted(placeholders))
        )
    return _FrameTemplate(tuple(tokens), frozenset(placeholders))


def _parse_night_light(value: Any, *, source: str) -> NightLightProfile:
    """Parse one optional night-light capability block."""

    commands = _require_object(value, _NIGHT_LIGHT_KEYS, source=source)
    polling_source = f"{source}.polling"
    polling = _require_object(
        commands["polling"], _NIGHT_LIGHT_POLLING_KEYS, source=polling_source
    )
    try:
        cadence = NightLightPollingCadence(polling["cadence"])
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"{polling_source}.cadence must be one of "
            + ", ".join(value.value for value in NightLightPollingCadence)
        ) from err
    interval_seconds = polling["interval_seconds"]
    if type(interval_seconds) is not int or not 3 <= interval_seconds <= 3600:
        raise ValueError(
            f"{polling_source}.interval_seconds must be from 3 to 3600 seconds"
        )
    timeout_seconds = polling["timeout_seconds"]
    if type(timeout_seconds) is not int or not 1 <= timeout_seconds <= 5:
        raise ValueError(
            f"{polling_source}.timeout_seconds must be from 1 to 5 seconds"
        )
    try:
        request_order = NightLightPollingRequestOrder(polling["request_order"])
    except (TypeError, ValueError) as err:
        raise ValueError(
            f"{polling_source}.request_order must be one of "
            + ", ".join(value.value for value in NightLightPollingRequestOrder)
        ) from err
    max_backoff_seconds = polling["max_backoff_seconds"]
    if (
        type(max_backoff_seconds) is not int
        or not interval_seconds <= max_backoff_seconds <= 86400
    ):
        raise ValueError(
            f"{polling_source}.max_backoff_seconds must be from "
            f"{interval_seconds} to 86400 seconds"
        )
    power_off = _parse_frame(commands["power_off"], source=f"{source}.power_off")
    power_on = _parse_frame(commands["power_on"], source=f"{source}.power_on")
    power_brightness_query = _parse_frame(
        commands["power_brightness_query"],
        source=f"{source}.power_brightness_query",
    )
    brightness_template = _parse_frame_template(
        commands["brightness_template"],
        placeholders={"brightness"},
        source=f"{source}.brightness_template",
    )
    rgb_template = _parse_frame_template(
        commands["rgb_template"],
        placeholders={"red", "green", "blue"},
        source=f"{source}.rgb_template",
    )
    rgb_state_query = _parse_frame(
        commands["rgb_state_query"], source=f"{source}.rgb_state_query"
    )
    expected_values = {
        "power_off": (power_off, build_frame(bytes.fromhex("3a 1b 01 01 00"))),
        "power_on": (power_on, build_frame(bytes.fromhex("3a 1b 01 01 01"))),
        "power_brightness_query": (
            power_brightness_query,
            build_frame(bytes.fromhex("aa 1b 01")),
        ),
        "rgb_state_query": (
            rgb_state_query,
            build_frame(bytes.fromhex("aa 1b 05")),
        ),
    }
    for key, (actual, expected) in expected_values.items():
        if actual != expected:
            raise ValueError(f"{source}.{key} has an unexpected night-light layout")
    if brightness_template.tokens != (0x3A, 0x1B, 0x01, 0x02, "brightness"):
        raise ValueError(
            f"{source}.brightness_template has an unexpected night-light layout"
        )
    if rgb_template.tokens != (
        0x3A,
        0x1B,
        0x05,
        0x0D,
        "red",
        "green",
        "blue",
    ):
        raise ValueError(f"{source}.rgb_template has an unexpected night-light layout")
    return NightLightProfile(
        polling=NightLightPollingProfile(
            cadence=cadence,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            request_order=request_order,
            max_backoff_seconds=max_backoff_seconds,
        ),
        power_off_command=power_off,
        power_on_command=power_on,
        power_brightness_query_command=power_brightness_query,
        brightness_template=brightness_template,
        rgb_template=rgb_template,
        rgb_state_query_command=rgb_state_query,
    )


def _parse_custom_auto(value: Any, *, source: str) -> tuple[int, int, int, int]:
    """Parse model-specific Custom Auto PM2.5 boundaries."""

    custom_auto = _require_object(value, _CUSTOM_AUTO_KEYS, source=source)
    thresholds = custom_auto["thresholds"]
    if not isinstance(thresholds, list) or len(thresholds) != 4:
        raise ValueError(f"{source}.thresholds must contain exactly four values")
    if any(
        type(threshold) is not int or not 0 <= threshold <= 999
        for threshold in thresholds
    ):
        raise ValueError(f"{source}.thresholds must contain integers from 0 to 999")
    if not all(
        left < right for left, right in zip(thresholds, thresholds[1:], strict=False)
    ):
        raise ValueError(f"{source}.thresholds must be strictly ascending")
    return thresholds[0], thresholds[1], thresholds[2], thresholds[3]


def _parse_push_notifications(
    value: Any,
    *,
    source: str,
    has_night_light: bool,
) -> PushNotificationProfile:
    """Parse profile-gated unsolicited notification capabilities."""

    push = _require_object(value, _PUSH_NOTIFICATION_KEYS, source=source)
    for key, enabled in push.items():
        if type(enabled) is not bool:
            raise ValueError(f"{source}.{key} must be a boolean")
    if push["night_light_power_brightness"] and not has_night_light:
        raise ValueError(
            f"{source}.night_light_power_brightness requires night_light capability"
        )
    return PushNotificationProfile(
        power_state=push["power_state"],
        fan_mode=push["fan_mode"],
        night_light_power_brightness=push["night_light_power_brightness"],
    )


def _parse_encryption(value: Any, *, source: str) -> EncryptionMode:
    """Return one supported profile encryption mode."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a string")
    try:
        return EncryptionMode(value)
    except ValueError as err:
        supported = ", ".join(mode.value for mode in EncryptionMode)
        raise ValueError(f"{source} must be one of {supported}") from err


def _parse_support_status(value: Any, *, source: str) -> ModelSupportStatus:
    """Return one supported model evidence level."""

    if not isinstance(value, str):
        raise ValueError(f"{source} must be a string")
    try:
        return ModelSupportStatus(value)
    except ValueError as err:
        supported = ", ".join(status.value for status in ModelSupportStatus)
        raise ValueError(f"{source} must be one of {supported}") from err


def _parse_polling_interval(value: Any, *, source: str) -> int:
    """Return one supported profile polling interval."""

    if type(value) is not int or not (
        MIN_POLLING_INTERVAL_SECONDS <= value <= MAX_POLLING_INTERVAL_SECONDS
    ):
        raise ValueError(
            f"{source} must be an integer from "
            f"{MIN_POLLING_INTERVAL_SECONDS} to {MAX_POLLING_INTERVAL_SECONDS}"
        )
    return value


def _parse_profile_definition(data: Any, *, source: str) -> _ProfileDefinition:
    """Validate and normalize one complete profile definition."""

    profile = _require_object(
        data,
        _TOP_LEVEL_KEYS,
        source=source,
        optional_keys=_TOP_LEVEL_OPTIONAL_KEYS,
    )
    if (
        type(profile["schema_version"]) is not int
        or profile["schema_version"] != PROFILE_SCHEMA_VERSION
    ):
        raise ValueError(f"{source}.schema_version must be {PROFILE_SCHEMA_VERSION}")
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

    night_light = (
        _parse_night_light(profile["night_light"], source=f"{source}.night_light")
        if "night_light" in profile
        else None
    )

    return _ProfileDefinition(
        support_status=_parse_support_status(
            profile["support_status"], source=f"{source}.support_status"
        ),
        polling_interval_seconds=_parse_polling_interval(
            profile["polling_interval_seconds"],
            source=f"{source}.polling_interval_seconds",
        ),
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
        custom_auto_thresholds=(
            _parse_custom_auto(profile["custom_auto"], source=f"{source}.custom_auto")
            if "custom_auto" in profile
            else None
        ),
        night_light=night_light,
        push_notifications=(
            _parse_push_notifications(
                profile["push_notifications"],
                source=f"{source}.push_notifications",
                has_night_light=night_light is not None,
            )
            if "push_notifications" in profile
            else None
        ),
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
            raise ValueError(
                f"Unable to load model profile {path.name}: {err}"
            ) from err
        data = _decode_profile_json(raw, source=path.name)
        definitions[key] = _parse_profile_definition(data, source=path.name)
    if DEFAULT_PROFILE_KEY not in definitions:
        raise ValueError("Missing model_profiles/default.json")
    if H7124_PROFILE_KEY not in definitions:
        raise ValueError("Missing model_profiles/h7124.json")
    if (
        definitions[DEFAULT_PROFILE_KEY].support_status
        is not ModelSupportStatus.FALLBACK
    ):
        raise ValueError("model_profiles/default.json support_status must be fallback")
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
    is_exact_profile = resolved_key in definitions
    definition = definitions.get(resolved_key, definitions[DEFAULT_PROFILE_KEY])
    return ModelProfile(
        key=resolved_key,
        model=model,
        display_name=f"Govee {model} Air Purifier",
        local_name_prefixes=(f"GV{model}",),
        support_status=(
            definition.support_status
            if is_exact_profile
            else ModelSupportStatus.FALLBACK
        ),
        polling_interval_seconds=definition.polling_interval_seconds,
        encryption=definition.encryption,
        service_uuid=definition.service_uuid,
        notify_char_uuid=definition.notify_char_uuid,
        write_char_uuid=definition.write_char_uuid,
        power_off_command=definition.power_off_command,
        power_on_command=definition.power_on_command,
        state_query_command=definition.state_query_command,
        status_query_command=definition.status_query_command,
        fan_mode_commands=dict(definition.fan_mode_commands),
        custom_auto_thresholds=definition.custom_auto_thresholds,
        night_light=definition.night_light,
        push_notifications=definition.push_notifications,
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
