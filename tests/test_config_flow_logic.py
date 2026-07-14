from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.const import (
    CONF_POLLING_INTERVAL,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from custom_components.govee_ble_air_purifier.controller import (
    CUSTOM_AUTO_DEFAULTS,
    CustomAutoConfig,
    parse_custom_auto_values,
    validate_custom_auto_values,
)
from custom_components.govee_ble_air_purifier.setup_helpers import (
    MANUAL_DEVICE_VALUE,
    build_discovered_device_options,
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
            _service_info(
                "GVH7124NEAR", "AA:BB:CC:DD:EE:01", rssi=-43, source="hci0"
            ),
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


def test_discovered_device_options_deduplicate_by_address_using_strongest_signal() -> None:
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
    assert build_discovered_device_options(
        [_service_info("GVH7124BAD", "AA:BB:CC:DD:EE:FFG")]
    ) == ()


def test_polling_interval_validation_accepts_configured_bounds() -> None:
    assert MIN_POLLING_INTERVAL_SECONDS == 5
    assert validate_polling_interval_seconds(MIN_POLLING_INTERVAL_SECONDS) == 5
    assert validate_polling_interval_seconds("45") == 45
    assert validate_polling_interval_seconds(MAX_POLLING_INTERVAL_SECONDS) == 300
    assert DEFAULT_POLLING_INTERVAL_SECONDS == 10


@pytest.mark.parametrize("value", [4, 301, "not-a-number"])
def test_polling_interval_validation_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_polling_interval_seconds(value)


def test_polling_interval_from_options_defaults_when_missing_or_invalid() -> None:
    assert polling_interval_from_options({}) == DEFAULT_POLLING_INTERVAL_SECONDS
    assert polling_interval_from_options({CONF_POLLING_INTERVAL: 15}) == 15
    assert polling_interval_from_options({CONF_POLLING_INTERVAL: 120}) == 120
    assert (
        polling_interval_from_options({CONF_POLLING_INTERVAL: "not-a-number"})
        == DEFAULT_POLLING_INTERVAL_SECONDS
    )


def test_custom_auto_options_default_for_existing_entries() -> None:
    config = CustomAutoConfig.from_options({})

    assert config.up_thresholds == (3, 5, 9, 15)
    assert config.down_thresholds == (3, 5, 9, 14)
    assert config.down_delays == (7, 5, 5, 5)


def test_custom_auto_options_parse_every_mutable_value() -> None:
    values = {
        key: value + 1 for key, value in CUSTOM_AUTO_DEFAULTS.items()
    }
    config = CustomAutoConfig.from_options({"use_custom_auto": True, **values})

    assert config.as_options() == values


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        (
            {"custom_auto_up_60": 3},
            "up_thresholds_not_ascending",
        ),
        (
            {"custom_auto_down_40": 3},
            "down_thresholds_not_ascending",
        ),
        (
            {"custom_auto_down_80": 16},
            "down_threshold_above_up",
        ),
    ],
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
        ("custom_auto_up_40", -1),
        ("custom_auto_up_100", 1000),
        ("custom_auto_delay_20", -1),
        ("custom_auto_delay_80", 1441),
        ("custom_auto_up_40", 3.5),
    ],
)
def test_custom_auto_value_parsing_rejects_out_of_range_or_non_integer_values(
    key: str, value: object
) -> None:
    with pytest.raises(ValueError):
        parse_custom_auto_values({**CUSTOM_AUTO_DEFAULTS, key: value})
