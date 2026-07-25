"""Tests for pure Custom Auto speed policy."""

from custom_components.govee_ble_air_purifier.custom_auto.policy import (
    CUSTOM_AUTO_SPEEDS,
    MODE_TO_SPEED,
    SPEED_TO_MODE,
    UPSHIFT_CONFIRMATION_SAMPLES,
    speed_for_pm,
)


def test_policy_constants_and_mappings_are_exact() -> None:
    assert CUSTOM_AUTO_SPEEDS == (20, 40, 60, 80, 100)
    assert UPSHIFT_CONFIRMATION_SAMPLES == 2
    assert SPEED_TO_MODE == {
        20: "Sleep",
        40: "Low",
        60: "Medium",
        80: "High",
        100: "Turbo",
    }
    assert MODE_TO_SPEED == {
        "Sleep": 20,
        "Low": 40,
        "Medium": 60,
        "High": 80,
        "Turbo": 100,
    }


def test_speed_threshold_boundaries_remain_strict() -> None:
    thresholds = (3, 5, 9, 15)

    assert [speed_for_pm(pm25, thresholds) for pm25 in (3, 4, 5, 6, 9, 10, 15, 16)] == [
        20,
        40,
        40,
        60,
        60,
        80,
        80,
        100,
    ]
