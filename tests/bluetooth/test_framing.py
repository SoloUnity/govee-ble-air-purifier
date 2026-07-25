import pytest

from custom_components.govee_ble_air_purifier.bluetooth.framing import (
    ProtocolError,
    build_frame,
    validate_frame,
)


def test_build_frame_pads_to_20_bytes_and_adds_xor_checksum() -> None:
    assert build_frame(bytes.fromhex("33 01 01")) == bytes.fromhex(
        "33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33"
    )


def test_build_frame_rejects_oversized_payload() -> None:
    with pytest.raises(ProtocolError, match="must fit in the first 19 bytes"):
        build_frame(bytes(20))


def test_validate_frame_rejects_bad_length_and_checksum() -> None:
    with pytest.raises(ProtocolError):
        validate_frame(b"too short")

    bad_checksum = bytearray(build_frame(bytes.fromhex("33 01 01")))
    bad_checksum[-1] = 0x00
    with pytest.raises(ProtocolError):
        validate_frame(bytes(bad_checksum))
