"""Regression tests backed by sanitized, traceable protocol captures."""

from __future__ import annotations

from copy import deepcopy
import json

import pytest

from custom_components.govee_ble_air_purifier.bluetooth.framing import validate_frame
from custom_components.govee_ble_air_purifier.bluetooth.govee_v1 import (
    decrypt_frame,
    encrypt_frame,
)
from custom_components.govee_ble_air_purifier.models import (
    DecodedStatus,
    NightLightState,
)
from custom_components.govee_ble_air_purifier.profiles import get_profile
from custom_components.govee_ble_air_purifier.protocol import (
    decode_mode_push,
    decode_night_light_power_brightness,
    decode_night_light_power_brightness_push,
    decode_night_light_rgb_state,
    is_command_echo,
)
from tests.helpers.protocol_fixtures import (
    FIXTURE_ROOT,
    ProtocolCaptureFixture,
    ProtocolFixtureError,
    ProtocolFrameFixture,
    load_all_protocol_fixtures,
    load_protocol_fixture,
    parse_protocol_fixture,
)

# Deliberately synthetic and non-secret. Captured H7129 session material does not
# belong in the fixture corpus; round trips still exercise the production cipher.
SYNTHETIC_TEST_KEY = bytes(range(16))
CORPUS = load_all_protocol_fixtures()
FRAME_CASES = tuple(
    (fixture, frame) for fixture in CORPUS for frame in fixture.frames
)


def _assert_expected_interpretation(
    fixture: ProtocolCaptureFixture, frame: ProtocolFrameFixture
) -> None:
    """Run the production decoder named by one fixture record."""

    expected = frame.expected
    decoder = expected["decoder"]
    profile = get_profile(fixture.model.lower())

    if decoder == "power_state":
        assert profile.decode_power_state(frame.plaintext) is expected["is_on"]
    elif decoder == "status":
        assert profile.decode_status(frame.plaintext) == DecodedStatus(
            pm25=expected["pm25"],
            filter_life=expected["filter_life"],
        )
    elif decoder == "fan_mode_push":
        assert (
            decode_mode_push(frame.plaintext, profile.fan_mode_commands)
            == expected["mode"]
        )
    elif decoder == "night_light_power_brightness":
        assert decode_night_light_power_brightness(frame.plaintext) == NightLightState(
            is_on=expected["is_on"],
            brightness_percent=expected["brightness_percent"],
        )
    elif decoder == "night_light_power_brightness_push":
        assert decode_night_light_power_brightness_push(
            frame.plaintext
        ) == NightLightState(
            is_on=expected["is_on"],
            brightness_percent=expected["brightness_percent"],
        )
    elif decoder == "night_light_rgb_state":
        rgb = expected["rgb"]
        assert decode_night_light_rgb_state(frame.plaintext) == (
            tuple(rgb) if isinstance(rgb, list) else None
        )
    elif decoder == "profile_command":
        assert profile.fan_mode_commands[expected["profile_command"]] == frame.plaintext
    elif decoder == "command_echo":
        assert is_command_echo(frame.plaintext, frame.plaintext)
    else:  # pragma: no cover - the explicit failure documents the fixture contract.
        pytest.fail(f"Unsupported fixture decoder {decoder!r}")


def test_protocol_fixture_corpus_has_expected_models_and_transport() -> None:
    """Keep the checked-in corpus complete and transport metadata explicit."""

    assert [fixture.model for fixture in CORPUS] == ["H7124", "H7129"]
    assert CORPUS[0].wire_encryption == "none"
    assert CORPUS[1].wire_encryption == "govee_v1"
    assert all(fixture.application_frame_encoding == "plaintext" for fixture in CORPUS)


def test_protocol_fixture_provenance_preserves_documented_capture_hashes() -> None:
    """Retain safe capture identity without storing the raw capture itself."""

    captures = {
        provenance.capture_sha256
        for fixture in CORPUS
        for provenance in fixture.provenance
        if provenance.capture_sha256 is not None
    }

    assert captures == {
        "1f71ead53c29bd2d24493619e44502e2197faa91f12bf8c31b5c90aeda66c242",
        "9fb7a73cebee327dd0290473aea45d7caa7317a389134379552a7308cc83a177",
    }


@pytest.mark.parametrize(
    ("fixture", "frame"),
    FRAME_CASES,
    ids=[f"{fixture.model}-{frame.id}" for fixture, frame in FRAME_CASES],
)
def test_sanitized_capture_frames_against_production_protocol(
    fixture: ProtocolCaptureFixture, frame: ProtocolFrameFixture
) -> None:
    """Feed every capture-derived plaintext frame through production code."""

    validate_frame(frame.plaintext)
    _assert_expected_interpretation(fixture, frame)


@pytest.mark.parametrize(
    "frame",
    load_protocol_fixture("h7129").frames,
    ids=lambda frame: frame.id,
)
def test_h7129_capture_plaintext_round_trips_govee_v1_with_synthetic_key(
    frame: ProtocolFrameFixture,
) -> None:
    """Exercise encrypted transport without retaining captured session keys."""

    wire_frame = encrypt_frame(frame.plaintext, SYNTHETIC_TEST_KEY)

    assert wire_frame != frame.plaintext
    assert decrypt_frame(wire_frame, SYNTHETIC_TEST_KEY) == frame.plaintext


def test_fixture_loader_rejects_unredacted_metadata() -> None:
    """Fail closed if a contributor marks identifying or key material present."""

    data = json.loads((FIXTURE_ROOT / "h7124" / "protocol.json").read_text())
    unsafe = deepcopy(data)
    unsafe["sanitization"]["identifiers_removed"] = False

    with pytest.raises(ProtocolFixtureError, match="sanitization must attest"):
        parse_protocol_fixture(unsafe, source="unsafe-fixture")


def test_fixture_loader_rejects_frame_digest_drift() -> None:
    """Require an intentional digest update whenever fixture bytes change."""

    data = json.loads((FIXTURE_ROOT / "h7124" / "protocol.json").read_text())
    changed = deepcopy(data)
    changed["frames"][0]["plaintext_sha256"] = "0" * 64

    with pytest.raises(ProtocolFixtureError, match="does not match the frame"):
        parse_protocol_fixture(changed, source="changed-fixture")
