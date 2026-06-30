from types import SimpleNamespace

import pytest

from custom_components.govee_ble_air_purifier.const import (
    CONF_POLLING_INTERVAL,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from custom_components.govee_ble_air_purifier.setup_helpers import (
    MANUAL_DEVICE_VALUE,
    build_discovered_device_options,
    polling_interval_from_options,
    validate_polling_interval_seconds,
)


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


def test_polling_interval_validation_accepts_configured_bounds() -> None:
    assert MIN_POLLING_INTERVAL_SECONDS == 5
    assert validate_polling_interval_seconds(MIN_POLLING_INTERVAL_SECONDS) == 5
    assert validate_polling_interval_seconds("45") == 45
    assert validate_polling_interval_seconds(MAX_POLLING_INTERVAL_SECONDS) == 300


@pytest.mark.parametrize("value", [4, 301, "not-a-number"])
def test_polling_interval_validation_rejects_invalid_values(value: object) -> None:
    with pytest.raises(ValueError):
        validate_polling_interval_seconds(value)


def test_polling_interval_from_options_defaults_when_missing_or_invalid() -> None:
    assert polling_interval_from_options({}) == DEFAULT_POLLING_INTERVAL_SECONDS
    assert polling_interval_from_options({CONF_POLLING_INTERVAL: 120}) == 120
    assert (
        polling_interval_from_options({CONF_POLLING_INTERVAL: "not-a-number"})
        == DEFAULT_POLLING_INTERVAL_SECONDS
    )
