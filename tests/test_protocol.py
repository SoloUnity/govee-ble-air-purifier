import pytest

from custom_components.govee_ble_air_purifier.models import GoveeAirPurifierState
from custom_components.govee_ble_air_purifier.profiles import (
    H7124_PROFILE,
    fan_mode_labels,
    match_profile,
    normalize_ble_address,
)
from custom_components.govee_ble_air_purifier.protocol import (
    FAN_MODE_COMMANDS,
    FAN_MODE_LABELS,
    POWER_OFF_COMMAND,
    POWER_ON_COMMAND,
    STATE_QUERY_COMMAND,
    STATUS_QUERY_COMMAND,
    ProtocolError,
    build_frame,
    decode_power_state,
    decode_status,
    normalize_ble_name,
    validate_frame,
)


def test_build_frame_pads_to_20_bytes_and_adds_xor_checksum() -> None:
    assert build_frame(bytes.fromhex("33 01 01")) == bytes.fromhex(
        "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33"
    )


@pytest.mark.parametrize(
    ("constant", "expected"),
    [
        (POWER_OFF_COMMAND, "33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32"),
        (POWER_ON_COMMAND, "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33"),
        (STATE_QUERY_COMMAND, "aa 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ab"),
        (STATUS_QUERY_COMMAND, "aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3"),
    ],
)
def test_power_and_query_commands_are_exact_captures(
    constant: bytes, expected: str
) -> None:
    assert constant == bytes.fromhex(expected)
    validate_frame(constant)


def test_fan_mode_options_exclude_off_and_commands_are_canonical() -> None:
    assert FAN_MODE_LABELS == ["Low", "Medium", "High", "Sleep", "Auto", "Turbo"]
    assert FAN_MODE_COMMANDS == {
        "Low": bytes.fromhex("3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f"),
        "Medium": bytes.fromhex("3a 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3c"),
        "High": bytes.fromhex("3a 05 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3d"),
        "Sleep": bytes.fromhex("3a 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3a"),
        "Auto": bytes.fromhex("3a 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 28"),
        "Turbo": bytes.fromhex("3a 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 38"),
    }


def test_decode_power_state_from_aa01_response() -> None:
    assert decode_power_state(
        bytes.fromhex("aa 01 00 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2a")
    ) is False
    assert decode_power_state(
        bytes.fromhex("aa 01 01 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2b")
    ) is True


def test_decode_status_uses_big_endian_pm25_and_filter_percent() -> None:
    state = decode_status(
        bytes.fromhex("aa 19 81 03 82 01 00 64 00 00 00 00 00 00 00 00 00 00 00 d6")
    )
    assert state == GoveeAirPurifierState(pm25=898, filter_life=100)


def test_validate_frame_rejects_bad_length_and_checksum() -> None:
    with pytest.raises(ProtocolError):
        validate_frame(b"too short")

    bad_checksum = bytearray(POWER_ON_COMMAND)
    bad_checksum[-1] = 0x00
    with pytest.raises(ProtocolError):
        validate_frame(bytes(bad_checksum))


def test_ble_name_normalization_accepts_h7124_prefix() -> None:
    assert normalize_ble_name("GVH712438FE") == "H7124-38FE"
    assert normalize_ble_name("GVH7124178E") == "H7124-178E"
    assert normalize_ble_name("Other") is None


def test_profile_lookup_matches_h7124_ble_names() -> None:
    assert match_profile("GVH712438FE") is H7124_PROFILE
    assert match_profile("GVH7124") is H7124_PROFILE
    assert match_profile("Other") is None


def test_h7124_profile_exposes_exact_protocol_frames() -> None:
    assert H7124_PROFILE.service_uuid == "00010203-0405-0607-0809-0a0b0c0d1910"
    assert H7124_PROFILE.notify_char_uuid == "00010203-0405-0607-0809-0a0b0c0d2b10"
    assert H7124_PROFILE.write_char_uuid == "00010203-0405-0607-0809-0a0b0c0d2b11"
    assert H7124_PROFILE.power_off_command == POWER_OFF_COMMAND
    assert H7124_PROFILE.power_on_command == POWER_ON_COMMAND
    assert H7124_PROFILE.state_query_command == STATE_QUERY_COMMAND
    assert H7124_PROFILE.status_query_command == STATUS_QUERY_COMMAND
    assert H7124_PROFILE.fan_mode_commands == FAN_MODE_COMMANDS
    assert fan_mode_labels(H7124_PROFILE) == [
        "Low",
        "Medium",
        "High",
        "Sleep",
        "Auto",
        "Turbo",
    ]


def test_unique_id_prefers_normalized_ble_address_not_name_suffix() -> None:
    assert normalize_ble_address("AA:BB:CC:DD:EE:FF") == "aabbccddeeff"
    assert normalize_ble_address("aa-bb-cc-dd-ee-ff") == "aabbccddeeff"
