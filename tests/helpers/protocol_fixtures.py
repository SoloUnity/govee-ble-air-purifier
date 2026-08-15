"""Load and validate sanitized protocol-capture regression fixtures."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from custom_components.govee_ble_air_purifier.bluetooth.framing import (
    ProtocolError,
    validate_frame,
)

FIXTURE_ROOT = Path(__file__).parents[1] / "fixtures"
FIXTURE_SCHEMA_VERSION = 1

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_DIRECTIONS = {"client_to_device", "device_to_client"}
_FRAME_ROLES = {"command", "query", "response", "push"}
_APPLICATION_FRAME_ENCODINGS = {"plaintext"}
_WIRE_ENCRYPTIONS = {"none", "govee_v1"}


class ProtocolFixtureError(ValueError):
    """Raised when a protocol fixture is malformed or unsafe."""


@dataclass(frozen=True, slots=True)
class FixtureProvenance:
    """One source from which sanitized fixture data was transcribed."""

    id: str
    source_type: str
    reference: str
    capture_sha256: str | None


@dataclass(frozen=True, slots=True)
class ProtocolFrameFixture:
    """One checksum-valid, sanitized plaintext application frame."""

    id: str
    direction: str
    frame_role: str
    plaintext: bytes
    plaintext_sha256: str
    provenance_id: str
    verification_level: str
    expected: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProtocolCaptureFixture:
    """Validated fixture corpus for one purifier model."""

    model: str
    protocol: str
    application_frame_encoding: str
    wire_encryption: str
    verification_level: str
    provenance: tuple[FixtureProvenance, ...]
    frames: tuple[ProtocolFrameFixture, ...]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Decode a JSON object without allowing silent duplicate keys."""

    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolFixtureError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _require_object(
    value: object,
    *,
    required: set[str],
    optional: set[str] | None = None,
    source: str,
) -> dict[str, Any]:
    """Return a JSON object with an exact, documented set of keys."""

    if not isinstance(value, dict):
        raise ProtocolFixtureError(f"{source} must be an object")
    optional = optional or set()
    missing = required - value.keys()
    unknown = value.keys() - required - optional
    if missing:
        raise ProtocolFixtureError(f"{source} is missing {sorted(missing)}")
    if unknown:
        raise ProtocolFixtureError(f"{source} has unknown keys {sorted(unknown)}")
    return value


def _require_string(value: object, *, source: str) -> str:
    """Return one non-empty, whitespace-normalized string."""

    if not isinstance(value, str) or not value or value != value.strip():
        raise ProtocolFixtureError(f"{source} must be a non-empty trimmed string")
    return value


def _require_sha256(value: object, *, source: str) -> str:
    """Return one lower-case SHA-256 digest."""

    digest = _require_string(value, source=source)
    if _SHA256_PATTERN.fullmatch(digest) is None:
        raise ProtocolFixtureError(f"{source} must be a lower-case SHA-256 digest")
    return digest


def _parse_provenance(value: object, *, source: str) -> tuple[FixtureProvenance, ...]:
    """Validate the fixture's capture/document provenance records."""

    if not isinstance(value, list) or not value:
        raise ProtocolFixtureError(f"{source} must be a non-empty array")
    parsed: list[FixtureProvenance] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        item_source = f"{source}[{index}]"
        record = _require_object(
            item,
            required={"id", "source_type", "reference"},
            optional={"capture_sha256"},
            source=item_source,
        )
        provenance_id = _require_string(record["id"], source=f"{item_source}.id")
        if provenance_id in seen_ids:
            raise ProtocolFixtureError(f"duplicate provenance id {provenance_id!r}")
        seen_ids.add(provenance_id)
        capture_sha256 = (
            _require_sha256(
                record["capture_sha256"], source=f"{item_source}.capture_sha256"
            )
            if "capture_sha256" in record
            else None
        )
        parsed.append(
            FixtureProvenance(
                id=provenance_id,
                source_type=_require_string(
                    record["source_type"], source=f"{item_source}.source_type"
                ),
                reference=_require_string(
                    record["reference"], source=f"{item_source}.reference"
                ),
                capture_sha256=capture_sha256,
            )
        )
    return tuple(parsed)


def _parse_frames(
    value: object,
    *,
    provenance_ids: set[str],
    source: str,
) -> tuple[ProtocolFrameFixture, ...]:
    """Validate and decode the sanitized application frames."""

    if not isinstance(value, list) or not value:
        raise ProtocolFixtureError(f"{source} must be a non-empty array")
    parsed: list[ProtocolFrameFixture] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(value):
        item_source = f"{source}[{index}]"
        record = _require_object(
            item,
            required={
                "id",
                "direction",
                "frame_role",
                "plaintext_hex",
                "plaintext_sha256",
                "provenance_id",
                "verification_level",
                "expected",
            },
            source=item_source,
        )
        frame_id = _require_string(record["id"], source=f"{item_source}.id")
        if frame_id in seen_ids:
            raise ProtocolFixtureError(f"duplicate frame id {frame_id!r}")
        seen_ids.add(frame_id)

        direction = _require_string(
            record["direction"], source=f"{item_source}.direction"
        )
        if direction not in _DIRECTIONS:
            raise ProtocolFixtureError(f"{item_source}.direction is unsupported")
        frame_role = _require_string(
            record["frame_role"], source=f"{item_source}.frame_role"
        )
        if frame_role not in _FRAME_ROLES:
            raise ProtocolFixtureError(f"{item_source}.frame_role is unsupported")

        plaintext_hex = _require_string(
            record["plaintext_hex"], source=f"{item_source}.plaintext_hex"
        )
        try:
            plaintext = bytes.fromhex(plaintext_hex)
        except ValueError as err:
            raise ProtocolFixtureError(
                f"{item_source}.plaintext_hex is not valid hexadecimal"
            ) from err
        try:
            validate_frame(plaintext)
        except ProtocolError as err:
            raise ProtocolFixtureError(
                f"{item_source}.plaintext_hex is not a valid application frame: {err}"
            ) from err

        plaintext_sha256 = _require_sha256(
            record["plaintext_sha256"], source=f"{item_source}.plaintext_sha256"
        )
        actual_digest = hashlib.sha256(plaintext).hexdigest()
        if plaintext_sha256 != actual_digest:
            raise ProtocolFixtureError(
                f"{item_source}.plaintext_sha256 does not match the frame"
            )

        provenance_id = _require_string(
            record["provenance_id"], source=f"{item_source}.provenance_id"
        )
        if provenance_id not in provenance_ids:
            raise ProtocolFixtureError(
                f"{item_source}.provenance_id refers to an unknown source"
            )
        expected = _require_object(
            record["expected"],
            required={"decoder", "interpretation"},
            optional={
                "brightness_percent",
                "filter_life",
                "is_on",
                "mode",
                "pm25",
                "profile_command",
                "rgb",
            },
            source=f"{item_source}.expected",
        )
        _require_string(expected["decoder"], source=f"{item_source}.expected.decoder")
        _require_string(
            expected["interpretation"],
            source=f"{item_source}.expected.interpretation",
        )
        parsed.append(
            ProtocolFrameFixture(
                id=frame_id,
                direction=direction,
                frame_role=frame_role,
                plaintext=plaintext,
                plaintext_sha256=plaintext_sha256,
                provenance_id=provenance_id,
                verification_level=_require_string(
                    record["verification_level"],
                    source=f"{item_source}.verification_level",
                ),
                expected=dict(expected),
            )
        )
    return tuple(parsed)


def parse_protocol_fixture(data: object, *, source: str) -> ProtocolCaptureFixture:
    """Validate decoded fixture JSON and return its typed representation."""

    document = _require_object(
        data,
        required={
            "schema_version",
            "model",
            "protocol",
            "transport",
            "verification_level",
            "sanitization",
            "provenance",
            "frames",
        },
        source=source,
    )
    if document["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise ProtocolFixtureError(
            f"{source}.schema_version must be {FIXTURE_SCHEMA_VERSION}"
        )

    transport = _require_object(
        document["transport"],
        required={"application_frame_encoding", "wire_encryption"},
        source=f"{source}.transport",
    )
    application_frame_encoding = _require_string(
        transport["application_frame_encoding"],
        source=f"{source}.transport.application_frame_encoding",
    )
    if application_frame_encoding not in _APPLICATION_FRAME_ENCODINGS:
        raise ProtocolFixtureError(
            f"{source}.transport.application_frame_encoding is unsupported"
        )
    wire_encryption = _require_string(
        transport["wire_encryption"],
        source=f"{source}.transport.wire_encryption",
    )
    if wire_encryption not in _WIRE_ENCRYPTIONS:
        raise ProtocolFixtureError(f"{source}.transport.wire_encryption is unsupported")

    sanitization = _require_object(
        document["sanitization"],
        required={
            "identifiers_removed",
            "timestamps_removed",
            "raw_capture_included",
            "key_material_included",
        },
        source=f"{source}.sanitization",
    )
    if sanitization != {
        "identifiers_removed": True,
        "timestamps_removed": True,
        "raw_capture_included": False,
        "key_material_included": False,
    }:
        raise ProtocolFixtureError(
            f"{source}.sanitization must attest that identifying, raw-capture, "
            "and key material is absent"
        )

    provenance = _parse_provenance(
        document["provenance"], source=f"{source}.provenance"
    )
    frames = _parse_frames(
        document["frames"],
        provenance_ids={record.id for record in provenance},
        source=f"{source}.frames",
    )
    return ProtocolCaptureFixture(
        model=_require_string(document["model"], source=f"{source}.model"),
        protocol=_require_string(document["protocol"], source=f"{source}.protocol"),
        application_frame_encoding=application_frame_encoding,
        wire_encryption=wire_encryption,
        verification_level=_require_string(
            document["verification_level"],
            source=f"{source}.verification_level",
        ),
        provenance=provenance,
        frames=frames,
    )


def load_protocol_fixture(
    model: str, *, fixture_root: Path = FIXTURE_ROOT
) -> ProtocolCaptureFixture:
    """Load one model's sanitized protocol regression fixture."""

    normalized_model = model.lower()
    if re.fullmatch(r"h[0-9a-f]{4}", normalized_model) is None:
        raise ProtocolFixtureError(f"unsupported fixture model {model!r}")
    path = fixture_root / normalized_model / "protocol.json"
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw, object_pairs_hook=_reject_duplicate_keys)
    except OSError as err:
        raise ProtocolFixtureError(f"unable to read {path}: {err}") from err
    except json.JSONDecodeError as err:
        raise ProtocolFixtureError(f"unable to decode {path}: {err}") from err
    fixture = parse_protocol_fixture(data, source=str(path))
    if fixture.model.casefold() != normalized_model:
        raise ProtocolFixtureError(
            f"{path}.model must match its {normalized_model} directory"
        )
    return fixture


def load_all_protocol_fixtures(
    *, fixture_root: Path = FIXTURE_ROOT
) -> tuple[ProtocolCaptureFixture, ...]:
    """Load every checked-in model fixture in deterministic model order."""

    return tuple(
        load_protocol_fixture(path.parent.name, fixture_root=fixture_root)
        for path in sorted(fixture_root.glob("*/protocol.json"))
    )
