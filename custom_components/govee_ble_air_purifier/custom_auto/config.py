"""Configuration parsing and validation for Custom Auto control."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..const import (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
    CONF_CUSTOM_AUTO_DOWN_20,
    CONF_CUSTOM_AUTO_DOWN_40,
    CONF_CUSTOM_AUTO_DOWN_60,
    CONF_CUSTOM_AUTO_DOWN_80,
    CONF_CUSTOM_AUTO_UP_100,
    CONF_CUSTOM_AUTO_UP_40,
    CONF_CUSTOM_AUTO_UP_60,
    CONF_CUSTOM_AUTO_UP_80,
)

DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS = 3
MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS = 300

CUSTOM_AUTO_DEFAULTS: dict[str, int] = {
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
    CONF_CUSTOM_AUTO_UP_40: 3,
    CONF_CUSTOM_AUTO_UP_60: 5,
    CONF_CUSTOM_AUTO_UP_80: 9,
    CONF_CUSTOM_AUTO_UP_100: 15,
    CONF_CUSTOM_AUTO_DOWN_80: 15,
    CONF_CUSTOM_AUTO_DELAY_80: 5,
    CONF_CUSTOM_AUTO_DOWN_60: 9,
    CONF_CUSTOM_AUTO_DELAY_60: 5,
    CONF_CUSTOM_AUTO_DOWN_40: 5,
    CONF_CUSTOM_AUTO_DELAY_40: 5,
    CONF_CUSTOM_AUTO_DOWN_20: 3,
    CONF_CUSTOM_AUTO_DELAY_20: 7,
}

UP_THRESHOLD_KEYS = (
    CONF_CUSTOM_AUTO_UP_40,
    CONF_CUSTOM_AUTO_UP_60,
    CONF_CUSTOM_AUTO_UP_80,
    CONF_CUSTOM_AUTO_UP_100,
)
DOWN_THRESHOLD_KEYS = (
    CONF_CUSTOM_AUTO_DOWN_20,
    CONF_CUSTOM_AUTO_DOWN_40,
    CONF_CUSTOM_AUTO_DOWN_60,
    CONF_CUSTOM_AUTO_DOWN_80,
)
DOWN_DELAY_KEYS = (
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
)
CUSTOM_AUTO_OPTION_KEYS = (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    *UP_THRESHOLD_KEYS,
    *DOWN_THRESHOLD_KEYS,
    *DOWN_DELAY_KEYS,
)


@dataclass(frozen=True)
class CustomAutoConfig:
    """Validated custom-auto configuration."""

    confirmation_delay_seconds: int
    up_thresholds: tuple[int, int, int, int]
    down_thresholds: tuple[int, int, int, int]
    down_delays: tuple[int, int, int, int]

    @classmethod
    def from_options(cls, options: Mapping[str, Any]) -> "CustomAutoConfig":
        """Read options, falling back safely for old or malformed entries."""

        try:
            values = parse_custom_auto_values(options)
            validate_custom_auto_values(values)
        except ValueError:
            values = dict(CUSTOM_AUTO_DEFAULTS)
        return cls(
            confirmation_delay_seconds=values[CONF_CUSTOM_AUTO_CONFIRMATION_DELAY],
            up_thresholds=tuple(values[key] for key in UP_THRESHOLD_KEYS),
            down_thresholds=tuple(values[key] for key in DOWN_THRESHOLD_KEYS),
            down_delays=tuple(values[key] for key in DOWN_DELAY_KEYS),
        )

    def as_options(self) -> dict[str, int]:
        """Return the configuration in config-entry option form."""

        values = {
            CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: self.confirmation_delay_seconds
        }
        values.update(dict(zip(UP_THRESHOLD_KEYS, self.up_thresholds, strict=True)))
        values.update(
            dict(zip(DOWN_THRESHOLD_KEYS, self.down_thresholds, strict=True))
        )
        values.update(dict(zip(DOWN_DELAY_KEYS, self.down_delays, strict=True)))
        return values


def parse_custom_auto_values(values: Mapping[str, Any]) -> dict[str, int]:
    """Parse bounded integer rule values, applying defaults for missing fields."""

    parsed: dict[str, int] = {}
    for key in CUSTOM_AUTO_OPTION_KEYS:
        value = values.get(key, CUSTOM_AUTO_DEFAULTS[key])
        if isinstance(value, bool):
            raise ValueError(f"{key} must be an integer")
        try:
            number = int(value)
        except (TypeError, ValueError) as err:
            raise ValueError(f"{key} must be an integer") from err
        if number != value and not (isinstance(value, str) and str(number) == value):
            raise ValueError(f"{key} must be an integer")
        if key == CONF_CUSTOM_AUTO_CONFIRMATION_DELAY:
            maximum = MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS
        else:
            maximum = 1440 if key in DOWN_DELAY_KEYS else 999
        if not 0 <= number <= maximum:
            raise ValueError(f"{key} is outside its allowed range")
        parsed[key] = number
    return parsed


def validate_custom_auto_values(values: Mapping[str, int]) -> None:
    """Validate threshold ordering and hysteresis relationships."""

    up = tuple(values[key] for key in UP_THRESHOLD_KEYS)
    down = tuple(values[key] for key in DOWN_THRESHOLD_KEYS)
    if not all(left < right for left, right in zip(up, up[1:])):
        raise ValueError("up_thresholds_not_ascending")
    if not all(left < right for left, right in zip(down, down[1:])):
        raise ValueError("down_thresholds_not_ascending")
    if any(down_value > up_value for down_value, up_value in zip(down, up)):
        raise ValueError("down_threshold_above_up")
