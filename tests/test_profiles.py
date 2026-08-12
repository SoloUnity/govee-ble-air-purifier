import copy
import json
from pathlib import Path

import pytest

from custom_components.govee_ble_air_purifier.profiles import (
    EncryptionMode,
    H7124_PROFILE,
    PROFILE_SCHEMA_VERSION,
    _build_profile,
    _load_profile_definitions,
    _parse_profile_definition,
    get_profile,
    match_profile,
    model_from_ble_name,
    normalize_ble_name,
)

ROOT = Path(__file__).resolve().parents[1]
PROFILE_DIRECTORY = (
    ROOT / "custom_components" / "govee_ble_air_purifier" / "model_profiles"
)


def _profile_data() -> dict:
    return json.loads((PROFILE_DIRECTORY / "h7124.json").read_text(encoding="utf-8"))


def _write_profile(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_bundled_h7124_definition_matches_default_fallback() -> None:
    h7124 = _profile_data()
    default = json.loads(
        (PROFILE_DIRECTORY / "default.json").read_text(encoding="utf-8")
    )

    assert h7124.pop("night_light") is not None
    assert h7124 == default
    assert h7124["schema_version"] == PROFILE_SCHEMA_VERSION


@pytest.mark.parametrize(
    ("name", "model", "normalized"),
    [
        ("GVH712438FE", "H7124", "H7124-38FE"),
        ("GVH7126LIVING", "H7126", "H7126-LIVING"),
        ("GVH712C", "H712C", "H712C"),
        ("ihoment_H7129_6A7D", "H7129", "H7129-6A7D"),
        ("prefix_h712c_suffix", "H712C", "H712C-suffix"),
    ],
)
def test_ble_names_resolve_family_model(name: str, model: str, normalized: str) -> None:
    assert model_from_ble_name(name) == model
    assert normalize_ble_name(name) == normalized
    profile = match_profile(name)
    assert profile is not None
    assert profile.key == model.lower()
    assert profile.model == model


@pytest.mark.parametrize(
    "name",
    [None, "", "GVH712", "ihoment_H712", "H7119", "H712\N{KELVIN SIGN}", "Other"],
)
def test_ble_names_reject_non_family_devices(name: str | None) -> None:
    assert model_from_ble_name(name) is None
    assert normalize_ble_name(name) is None
    assert match_profile(name) is None


def test_observed_ihoment_h7129_name_uses_exact_encrypted_profile() -> None:
    profile = match_profile("ihoment_H7129_6B51")

    assert profile is not None
    assert profile.key == "h7129"
    assert profile.model == "H7129"
    assert profile.matches_local_name("GVH7129BEDROOM")
    assert profile.matches_local_name("ihoment_H7129_6B51")
    assert not profile.matches_local_name("GVH7124BEDROOM")
    assert profile.encryption is EncryptionMode.GOVEE_V1
    assert profile.service_uuid == H7124_PROFILE.service_uuid
    assert profile.status_query_command == H7124_PROFILE.status_query_command
    assert profile.fan_mode_commands["Auto"] == bytes.fromhex(
        "3a 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 2e"
    )
    assert profile.supports_custom_auto is True
    assert profile.custom_auto_thresholds == (7, 9, 13, 19)
    assert profile.night_light is not None
    assert (
        profile.night_light.power_on_command
        == H7124_PROFILE.night_light.power_on_command
    )


def test_unbundled_family_model_uses_h7124_fallback_with_exact_identity() -> None:
    profile = get_profile("h7126")

    assert profile is get_profile("h7126")
    assert profile.key == "h7126"
    assert profile.model == "H7126"
    assert profile.display_name == "Govee H7126 Air Purifier"
    assert profile.local_name_prefixes == ("GVH7126",)
    assert profile.encryption is EncryptionMode.NONE
    assert profile.service_uuid == H7124_PROFILE.service_uuid
    assert profile.notify_char_uuid == H7124_PROFILE.notify_char_uuid
    assert profile.write_char_uuid == H7124_PROFILE.write_char_uuid
    assert profile.power_off_command == H7124_PROFILE.power_off_command
    assert profile.power_on_command == H7124_PROFILE.power_on_command
    assert profile.state_query_command == H7124_PROFILE.state_query_command
    assert profile.status_query_command == H7124_PROFILE.status_query_command
    assert profile.fan_mode_commands == H7124_PROFILE.fan_mode_commands
    assert profile.custom_auto_thresholds == H7124_PROFILE.custom_auto_thresholds
    assert profile.night_light is None


def test_legacy_profile_resolution_keeps_h7124_night_light_capability() -> None:
    assert get_profile(None) is H7124_PROFILE
    assert H7124_PROFILE.night_light is not None


def test_night_light_profile_builds_captured_dynamic_frames() -> None:
    night_light = H7124_PROFILE.night_light

    assert night_light is not None
    assert night_light.build_brightness_command(1) == bytes.fromhex(
        "3a 1b 01 02 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 23"
    )
    assert night_light.build_brightness_command(100) == bytes.fromhex(
        "3a 1b 01 02 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 46"
    )
    assert night_light.build_rgb_command((255, 255, 0)) == bytes.fromhex(
        "3a 1b 05 0d ff ff 00 00 00 00 00 00 00 00 00 00 00 00 00 29"
    )


@pytest.mark.parametrize("brightness", [True, 0, 101])
def test_night_light_profile_rejects_invalid_brightness(brightness: object) -> None:
    night_light = H7124_PROFILE.night_light

    assert night_light is not None
    with pytest.raises(ValueError, match="brightness must be from 1 to 100"):
        night_light.build_brightness_command(brightness)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "rgb_color",
    [(1, 2), (1, 2, 3, 4), (True, 0, 0), (-1, 0, 0), (256, 0, 0)],
)
def test_night_light_profile_rejects_invalid_rgb(rgb_color: tuple) -> None:
    night_light = H7124_PROFILE.night_light

    assert night_light is not None
    with pytest.raises(ValueError, match="Night-light RGB"):
        night_light.build_rgb_command(rgb_color)


def test_exact_model_definition_takes_precedence_over_default(tmp_path: Path) -> None:
    default = _profile_data()
    exact = copy.deepcopy(default)
    exact["gatt"]["service_uuid"] = "10010203-0405-0607-0809-0a0b0c0d1910"
    _write_profile(tmp_path / "default.json", default)
    _write_profile(tmp_path / "h7124.json", default)
    _write_profile(tmp_path / "h7126.json", exact)

    definitions = _load_profile_definitions(tmp_path)
    profile = _build_profile("h7126", definitions)

    assert profile.service_uuid == "10010203-0405-0607-0809-0a0b0c0d1910"
    assert profile.power_on_command == H7124_PROFILE.power_on_command


def test_malformed_exact_definition_does_not_fall_back(tmp_path: Path) -> None:
    default = _profile_data()
    malformed = copy.deepcopy(default)
    malformed["commands"]["power_on"] = "33 01 01"
    _write_profile(tmp_path / "default.json", default)
    _write_profile(tmp_path / "h7124.json", default)
    _write_profile(tmp_path / "h7126.json", malformed)

    with pytest.raises(ValueError, match="h7126.json.commands.power_on"):
        _load_profile_definitions(tmp_path)


def test_profile_loader_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    data = _profile_data()
    _write_profile(tmp_path / "default.json", data)
    _write_profile(tmp_path / "h7124.json", data)
    duplicate = json.dumps(data).replace(
        '"schema_version": 1',
        '"schema_version": 1, "schema_version": 1',
        1,
    )
    (tmp_path / "h7126.json").write_text(duplicate, encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate key schema_version"):
        _load_profile_definitions(tmp_path)


@pytest.mark.parametrize("key", ["h712", "h71244", "H7124", "../h7124", "other"])
def test_profile_lookup_rejects_invalid_persisted_keys(key: str) -> None:
    with pytest.raises(ValueError, match="Unsupported purifier profile"):
        get_profile(key)


def test_profile_schema_rejects_unknown_keys() -> None:
    data = _profile_data()
    data["unexpected"] = True

    with pytest.raises(ValueError, match="unknown unexpected"):
        _parse_profile_definition(data, source="test.json")


def test_profile_schema_allows_absent_optional_night_light() -> None:
    data = _profile_data()
    del data["night_light"]

    definition = _parse_profile_definition(data, source="test.json")

    assert definition.night_light is None


def test_profile_without_custom_auto_thresholds_does_not_support_policy() -> None:
    data = _profile_data()
    del data["custom_auto"]

    definition = _parse_profile_definition(data, source="test.json")
    profile = _build_profile("h7124", {"default": definition, "h7124": definition})

    assert profile.custom_auto_thresholds is None
    assert profile.supports_custom_auto is False


@pytest.mark.parametrize(
    ("thresholds", "message"),
    [
        ([3, 5, 9], "exactly four"),
        ([3, 5, 5, 15], "strictly ascending"),
        ([3, 5, 9, 1000], "integers from 0 to 999"),
        ([3, 5, 9, True], "integers from 0 to 999"),
    ],
)
def test_profile_schema_rejects_invalid_custom_auto_thresholds(
    thresholds: list[object], message: str
) -> None:
    data = _profile_data()
    data["custom_auto"]["thresholds"] = thresholds

    with pytest.raises(ValueError, match=message):
        _parse_profile_definition(data, source="test.json")


def test_profile_schema_rejects_incomplete_night_light() -> None:
    data = _profile_data()
    del data["night_light"]["rgb_state_query"]

    with pytest.raises(ValueError, match="missing rgb_state_query"):
        _parse_profile_definition(data, source="test.json")


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        (
            "brightness_template",
            "3a 1b 01 02 {level}",
            "unknown placeholder",
        ),
        (
            "brightness_template",
            "3a 1b 01 02 {brightness} {brightness}",
            "exactly once",
        ),
        (
            "rgb_template",
            "3a 1b 05 0d {red} {green}",
            "exactly once",
        ),
    ],
)
def test_profile_schema_rejects_invalid_night_light_templates(
    key: str, value: str, message: str
) -> None:
    data = _profile_data()
    data["night_light"][key] = value

    with pytest.raises(ValueError, match=message):
        _parse_profile_definition(data, source="test.json")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        (
            "power_on",
            "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33",
        ),
        (
            "brightness_template",
            "33 01 01 00 {brightness}",
        ),
        (
            "rgb_template",
            "3a 1b 05 0c {red} {green} {blue}",
        ),
    ],
)
def test_profile_schema_rejects_unexpected_night_light_layouts(
    key: str, value: str
) -> None:
    data = _profile_data()
    data["night_light"][key] = value

    with pytest.raises(ValueError, match="unexpected night-light layout"):
        _parse_profile_definition(data, source="test.json")


def test_profile_schema_rejects_invalid_uuid() -> None:
    data = _profile_data()
    data["gatt"]["service_uuid"] = "not-a-uuid"

    with pytest.raises(ValueError, match="is not a valid UUID"):
        _parse_profile_definition(data, source="test.json")


@pytest.mark.parametrize(
    "frame",
    [
        "33 01 01",
        "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00",
    ],
)
def test_profile_schema_rejects_invalid_frames(frame: str) -> None:
    data = copy.deepcopy(_profile_data())
    data["commands"]["power_on"] = frame

    with pytest.raises(ValueError, match="not a valid Govee BLE frame"):
        _parse_profile_definition(data, source="test.json")


@pytest.mark.parametrize("schema_version", [True, 0, 2, 3, "1"])
def test_profile_schema_rejects_unsupported_versions(schema_version: object) -> None:
    data = _profile_data()
    data["schema_version"] = schema_version

    with pytest.raises(ValueError, match="schema_version must be 1"):
        _parse_profile_definition(data, source="test.json")


@pytest.mark.parametrize("encryption", [True, "unknown"])
def test_profile_schema_rejects_invalid_encryption(encryption: object) -> None:
    data = _profile_data()
    data["encryption"] = encryption

    with pytest.raises(ValueError, match="test.json.encryption"):
        _parse_profile_definition(data, source="test.json")
