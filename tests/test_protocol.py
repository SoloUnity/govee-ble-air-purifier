import pytest

from custom_components.govee_ble_air_purifier.bluetooth.framing import (
    ProtocolError,
    build_frame,
    validate_frame,
)
from custom_components.govee_ble_air_purifier.models import DecodedStatus
from custom_components.govee_ble_air_purifier.profiles import (
    H7124_PROFILE,
    fan_mode_labels,
    normalize_ble_address,
    normalize_ble_name,
)
from custom_components.govee_ble_air_purifier.protocol import (
    decode_mode_push,
    decode_power_state,
    decode_status,
    is_command_echo,
    is_fan_mode_confirmation,
    is_power_confirmation,
)

FAN_MODE_COMMANDS = H7124_PROFILE.fan_mode_commands
FAN_MODE_LABELS = fan_mode_labels(H7124_PROFILE)
POWER_OFF_COMMAND = H7124_PROFILE.power_off_command
POWER_ON_COMMAND = H7124_PROFILE.power_on_command
STATE_QUERY_COMMAND = H7124_PROFILE.state_query_command
STATUS_QUERY_COMMAND = H7124_PROFILE.status_query_command

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


def test_power_query_echo_is_not_a_state_response() -> None:
    assert H7124_PROFILE.is_power_state_response(STATE_QUERY_COMMAND) is False
    with pytest.raises(ProtocolError, match="Not an aa01 power state response"):
        decode_power_state(STATE_QUERY_COMMAND)


def test_decode_status_uses_big_endian_pm25_and_filter_percent() -> None:
    state = decode_status(
        bytes.fromhex("aa 19 81 03 82 01 00 64 00 00 00 00 00 00 00 00 00 00 00 d6")
    )
    assert state == DecodedStatus(pm25=898, filter_life=100)


def test_status_query_echo_is_not_a_status_response() -> None:
    assert H7124_PROFILE.is_status_response(STATUS_QUERY_COMMAND) is False
    with pytest.raises(ProtocolError, match="Not an aa19 status response"):
        decode_status(STATUS_QUERY_COMMAND)


def test_decode_status_keeps_999_as_valid_pm25() -> None:
    state = decode_status(build_frame(bytes.fromhex("aa 19 81 03 e7 01 00 64")))

    assert state == DecodedStatus(pm25=999, filter_life=100)


@pytest.mark.parametrize("raw_pm25", [0x03E8, 0xFFFF])
def test_decode_status_treats_over_range_pm25_as_unknown(raw_pm25: int) -> None:
    frame = build_frame(
        bytes.fromhex("aa 19 81")
        + raw_pm25.to_bytes(2, "big")
        + bytes.fromhex("01 00 64")
    )

    assert decode_status(frame) == DecodedStatus(pm25=None, filter_life=100)


def test_power_confirmation_matches_requested_aa01_state() -> None:
    off_frame = bytes.fromhex(
        "aa 01 00 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2a"
    )
    on_frame = bytes.fromhex(
        "aa 01 01 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2b"
    )

    assert is_power_confirmation(off_frame, False) is True
    assert is_power_confirmation(on_frame, True) is True
    assert is_power_confirmation(off_frame, True) is False
    assert is_power_confirmation(FAN_MODE_COMMANDS["Low"], True) is False


def test_command_echo_requires_exact_command_frame() -> None:
    assert is_command_echo(FAN_MODE_COMMANDS["Low"], FAN_MODE_COMMANDS["Low"])
    assert not is_command_echo(FAN_MODE_COMMANDS["Low"], FAN_MODE_COMMANDS["High"])


@pytest.mark.parametrize(
    ("frame", "mode"),
    [
        (
            bytes.fromhex(
                "ee 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ee"
            ),
            "Sleep",
        ),
        (
            bytes.fromhex(
                "ee 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 fc"
            ),
            "Auto",
        ),
        (
            bytes.fromhex(
                "ee 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ec"
            ),
            "Turbo",
        ),
    ],
)
def test_decode_mode_push_for_modes_that_emit_ee05(frame: bytes, mode: str) -> None:
    assert decode_mode_push(frame) == mode
    assert is_fan_mode_confirmation(frame, mode, FAN_MODE_COMMANDS[mode])


def test_fan_mode_confirmation_accepts_exact_echo_for_all_modes() -> None:
    for mode, command in FAN_MODE_COMMANDS.items():
        assert is_fan_mode_confirmation(command, mode, command)

def test_ble_name_normalization_accepts_h7124_prefix() -> None:
    assert normalize_ble_name("GVH712438FE") == "H7124-38FE"
    assert normalize_ble_name("GVH7124178E") == "H7124-178E"
    assert normalize_ble_name("Other") is None
def test_h7124_profile_exposes_exact_protocol_frames() -> None:
    assert H7124_PROFILE.key == "h7124"
    assert H7124_PROFILE.model == "H7124"
    assert H7124_PROFILE.display_name == "Govee H7124 Air Purifier"
    assert H7124_PROFILE.local_name_prefixes == ("GVH7124",)
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
