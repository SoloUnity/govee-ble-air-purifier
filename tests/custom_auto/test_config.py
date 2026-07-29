"""Contracts for Custom Auto configuration parsing and validation."""

import pytest

from custom_components.govee_ble_air_purifier.const import (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
)
from custom_components.govee_ble_air_purifier.custom_auto.config import (
    CUSTOM_AUTO_DEFAULTS,
    CUSTOM_AUTO_OPTION_KEYS,
    DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
    MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
    CustomAutoConfig,
    parse_custom_auto_values,
    validate_custom_auto_values,
)


def test_option_order_is_stable() -> None:
    assert CUSTOM_AUTO_OPTION_KEYS == (
        "custom_auto_confirmation_delay",
        "custom_auto_up_40",
        "custom_auto_up_60",
        "custom_auto_up_80",
        "custom_auto_up_100",
        "custom_auto_down_20",
        "custom_auto_down_40",
        "custom_auto_down_60",
        "custom_auto_down_80",
        "custom_auto_delay_20",
        "custom_auto_delay_40",
        "custom_auto_delay_60",
        "custom_auto_delay_80",
    )


def test_default_key_order_is_stable() -> None:
    assert tuple(CUSTOM_AUTO_DEFAULTS) == (
        "custom_auto_confirmation_delay",
        "custom_auto_up_40",
        "custom_auto_up_60",
        "custom_auto_up_80",
        "custom_auto_up_100",
        "custom_auto_down_80",
        "custom_auto_delay_80",
        "custom_auto_down_60",
        "custom_auto_delay_60",
        "custom_auto_down_40",
        "custom_auto_delay_40",
        "custom_auto_down_20",
        "custom_auto_delay_20",
    )


def test_parser_applies_defaults_and_preserves_option_order() -> None:
    assert parse_custom_auto_values({}) == {
        key: CUSTOM_AUTO_DEFAULTS[key] for key in CUSTOM_AUTO_OPTION_KEYS
    }


@pytest.mark.parametrize(
    "value",
    [
        0,
        DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
        MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
    ],
)
def test_confirmation_delay_accepts_configured_bounds(value: int) -> None:
    parsed = parse_custom_auto_values({CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: value})

    assert parsed[CONF_CUSTOM_AUTO_CONFIRMATION_DELAY] == value


def test_confirmation_delay_rejects_value_above_configured_range() -> None:
    with pytest.raises(ValueError, match="outside its allowed range"):
        parse_custom_auto_values(
            {
                CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: (
                    MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS + 1
                )
            }
        )


@pytest.mark.parametrize("value", [0, 3, 3.0, "3", 999])
def test_parser_accepts_stable_integer_forms(value: object) -> None:
    parsed = parse_custom_auto_values({"custom_auto_up_40": value})

    assert parsed["custom_auto_up_40"] == int(value)


@pytest.mark.parametrize(
    ("value", "error"),
    [
        (True, "custom_auto_up_40 must be an integer"),
        (3.5, "custom_auto_up_40 must be an integer"),
        ("03", "custom_auto_up_40 must be an integer"),
        (None, "custom_auto_up_40 must be an integer"),
        (-1, "custom_auto_up_40 is outside its allowed range"),
        (1000, "custom_auto_up_40 is outside its allowed range"),
    ],
)
def test_parser_rejection_and_errors_are_stable(value: object, error: str) -> None:
    with pytest.raises(ValueError) as exc_info:
        parse_custom_auto_values({"custom_auto_up_40": value})

    assert str(exc_info.value) == error


def test_invalid_single_value_falls_back_to_complete_defaults() -> None:
    options = {
        **CUSTOM_AUTO_DEFAULTS,
        "custom_auto_up_40": 4,
        "custom_auto_up_60": "invalid",
    }

    assert CustomAutoConfig.from_options(options).as_options() == {
        key: CUSTOM_AUTO_DEFAULTS[key] for key in CUSTOM_AUTO_OPTION_KEYS
    }


@pytest.mark.parametrize(
    ("updates", "error"),
    [
        ({"custom_auto_up_60": 3}, "up_thresholds_not_ascending"),
        ({"custom_auto_down_40": 3}, "down_thresholds_not_ascending"),
        ({"custom_auto_down_80": 16}, "down_threshold_above_up"),
    ],
)
def test_validation_error_strings_are_stable(
    updates: dict[str, int], error: str
) -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_custom_auto_values({**CUSTOM_AUTO_DEFAULTS, **updates})

    assert str(exc_info.value) == error
