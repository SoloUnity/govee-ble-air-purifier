from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.const import (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    CONF_POLLING_INTERVAL,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from custom_components.govee_ble_air_purifier.custom_auto.config import (
    CUSTOM_AUTO_DEFAULTS,
    CustomAutoConfig,
    parse_custom_auto_values,
    validate_custom_auto_values,
)
from custom_components.govee_ble_air_purifier.setup_helpers import (
    MANUAL_DEVICE_VALUE,
    build_discovered_device_options,
    connection_sharing_from_options,
    polling_interval_from_options,
    validate_polling_interval_seconds,
)
from custom_components.govee_ble_air_purifier.profiles import canonicalize_ble_address


def _service_info(
    name: str | None,
    address: str,
    *,
    rssi: int | None = None,
    source: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(name=name, address=address, rssi=rssi, source=source)


def test_discovered_device_options_include_name_address_and_signal() -> None:
    options = build_discovered_device_options(
        [
            _service_info("GVH7124FAR", "AA:BB:CC:DD:EE:02", rssi=-86),
            _service_info("GVH7124NEAR", "AA:BB:CC:DD:EE:01", rssi=-43, source="hci0"),
            _service_info("Other", "AA:BB:CC:DD:EE:03", rssi=-20),
        ]
    )

    assert [option.value for option in options] == [
        "AA:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:02",
    ]
    assert options[0].name == "GVH7124NEAR"
    assert options[0].profile_key == "h7124"
    assert options[0].label == (
        "GVH7124NEAR (AA:BB:CC:DD:EE:01) - Very close signal (-43 dBm) via hci0"
    )
    assert options[1].label == (
        "GVH7124FAR (AA:BB:CC:DD:EE:02) - Very weak signal (-86 dBm)"
    )


def test_discovered_device_options_include_h712_family_fallback_models() -> None:
    options = build_discovered_device_options(
        [
            _service_info("ihoment_H7129_6A7D", "AA:BB:CC:DD:EE:09", rssi=-45),
            _service_info("GVH7126LIVING", "AA:BB:CC:DD:EE:06", rssi=-50),
            _service_info("ihoment_H7129_6B51", "AA:BB:CC:DD:EE:19", rssi=-55),
            _service_info("GVH712CBEDROOM", "AA:BB:CC:DD:EE:0C", rssi=-60),
            _service_info("GVH712", "AA:BB:CC:DD:EE:00", rssi=-40),
        ]
    )

    assert [option.profile_key for option in options] == [
        "h7129",
        "h7126",
        "h7129",
        "h712c",
    ]
    assert [option.name for option in options] == [
        "ihoment_H7129_6A7D",
        "GVH7126LIVING",
        "ihoment_H7129_6B51",
        "GVH712CBEDROOM",
    ]


def test_discovered_device_options_deduplicate_by_address_using_strongest_signal() -> (
    None
):
    options = build_discovered_device_options(
        [
            _service_info("GVH7124OLD", "AA:BB:CC:DD:EE:01", rssi=-82),
            _service_info("GVH7124NEW", "aa-bb-cc-dd-ee-01", rssi=-41),
        ]
    )

    assert len(options) == 1
    assert options[0].name == "GVH7124NEW"
    assert options[0].value == "aa-bb-cc-dd-ee-01"


def test_manual_device_option_is_a_stable_sentinel() -> None:
    assert MANUAL_DEVICE_VALUE == "__manual__"


@pytest.mark.parametrize(
    ("address", "canonical"),
    [
        ("aa:bb:cc:dd:ee:ff", "AA:BB:CC:DD:EE:FF"),
        ("aa-bb-cc-dd-ee-ff", "AA:BB:CC:DD:EE:FF"),
        (
            "A1B2C3D4-E5F6-47A8-9012-123456789ABC",
            "A1B2C3D4-E5F6-47A8-9012-123456789ABC",
        ),
    ],
)
def test_ble_address_validation_accepts_platform_formats(
    address: str, canonical: str
) -> None:
    assert canonicalize_ble_address(address) == canonical


@pytest.mark.parametrize(
    "address",
    [
        "AA:BB:CC:DD:EE:FFG",
        "AA:BB:CC:DD:EE",
        "AABBCCDDEEFF",
        "not-an-address",
    ],
)
def test_ble_address_validation_rejects_malformed_values(address: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_ble_address(address)


def test_discovered_device_options_ignore_malformed_addresses() -> None:
    assert (
        build_discovered_device_options(
            [_service_info("GVH7124BAD", "AA:BB:CC:DD:EE:FFG")]
        )
        == ()
    )


def test_polling_interval_validation_accepts_configured_bounds() -> None:
    assert MIN_POLLING_INTERVAL_SECONDS == 3
    assert validate_polling_interval_seconds(MIN_POLLING_INTERVAL_SECONDS) == 3
    assert validate_polling_interval_seconds("45") == 45
    assert validate_polling_interval_seconds(MAX_POLLING_INTERVAL_SECONDS) == 300


@pytest.mark.parametrize("value", [2, 301, "not-a-number"])
def test_polling_interval_validation_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_polling_interval_seconds(value)


def test_polling_interval_from_options_defaults_when_missing_or_invalid() -> None:
    assert polling_interval_from_options({}, 3) == 3
    assert polling_interval_from_options({}, 10) == 10
    assert polling_interval_from_options({CONF_POLLING_INTERVAL: 15}, 3) == 15
    assert polling_interval_from_options({CONF_POLLING_INTERVAL: 120}, 10) == 120
    assert (
        polling_interval_from_options(
            {CONF_POLLING_INTERVAL: "not-a-number"}, 3
        )
        == 3
    )


@pytest.mark.parametrize(
    ("options", "expected"),
    [
        ({}, False),
        ({"share_bluetooth_connection": False}, False),
        ({"share_bluetooth_connection": True}, True),
        ({"share_bluetooth_connection": "true"}, False),
        ({"share_bluetooth_connection": 1}, False),
    ],
)
def test_connection_sharing_from_options_is_explicitly_opt_in(
    options: dict[str, object], expected: bool
) -> None:
    assert connection_sharing_from_options(options) is expected


def test_custom_auto_options_default_for_existing_entries() -> None:
    config = CustomAutoConfig.from_options({})

    assert config.confirmation_delay_seconds == 3
    assert config.thresholds == (3, 5, 9, 15)
    assert config.down_delays == (7, 5, 5, 5)


def test_custom_auto_options_parse_every_mutable_value() -> None:
    values = {key: value + 1 for key, value in CUSTOM_AUTO_DEFAULTS.items()}
    config = CustomAutoConfig.from_options({"use_custom_auto": True, **values})

    assert config.as_options() == values


@pytest.mark.parametrize(
    ("updates", "error"),
    [({"custom_auto_threshold_60": 3}, "thresholds_not_ascending")],
)
def test_custom_auto_cross_validation_returns_stable_error_keys(
    updates: dict[str, int], error: str
) -> None:
    values = {**CUSTOM_AUTO_DEFAULTS, **updates}

    with pytest.raises(ValueError, match=error):
        validate_custom_auto_values(values)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("custom_auto_threshold_40", -1),
        ("custom_auto_threshold_100", 1000),
        ("custom_auto_delay_20", -1),
        ("custom_auto_delay_80", 1441),
        (CONF_CUSTOM_AUTO_CONFIRMATION_DELAY, 301),
        ("custom_auto_threshold_40", 3.5),
    ],
)
def test_custom_auto_value_parsing_rejects_out_of_range_or_non_integer_values(
    key: str, value: object
) -> None:
    with pytest.raises(ValueError):
        parse_custom_auto_values({**CUSTOM_AUTO_DEFAULTS, key: value})
