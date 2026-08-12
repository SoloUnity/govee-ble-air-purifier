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
    CONF_CUSTOM_AUTO_THRESHOLD_100,
    CONF_CUSTOM_AUTO_THRESHOLD_40,
    CONF_CUSTOM_AUTO_THRESHOLD_60,
    CONF_CUSTOM_AUTO_THRESHOLD_80,
)
from ..profiles import H7124_PROFILE

DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS = 3
MAX_UPSHIFT_CONFIRMATION_DELAY_SECONDS = 300

THRESHOLD_KEYS = (
    CONF_CUSTOM_AUTO_THRESHOLD_40,
    CONF_CUSTOM_AUTO_THRESHOLD_60,
    CONF_CUSTOM_AUTO_THRESHOLD_80,
    CONF_CUSTOM_AUTO_THRESHOLD_100,
)
DOWN_DELAY_KEYS = (
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
)
CUSTOM_AUTO_OPTION_KEYS = (
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    *THRESHOLD_KEYS,
    *DOWN_DELAY_KEYS,
)


def custom_auto_defaults(
    thresholds: tuple[int, int, int, int] | None,
) -> dict[str, int]:
    """Build complete Custom Auto defaults from model PM2.5 boundaries."""

    if thresholds is None:
        thresholds = H7124_PROFILE.custom_auto_thresholds
    assert thresholds is not None
    return {
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: DEFAULT_UPSHIFT_CONFIRMATION_DELAY_SECONDS,
        **dict(zip(THRESHOLD_KEYS, thresholds, strict=True)),
        CONF_CUSTOM_AUTO_DELAY_20: 7,
        CONF_CUSTOM_AUTO_DELAY_40: 5,
        CONF_CUSTOM_AUTO_DELAY_60: 5,
        CONF_CUSTOM_AUTO_DELAY_80: 5,
    }


CUSTOM_AUTO_DEFAULTS = custom_auto_defaults(H7124_PROFILE.custom_auto_thresholds)


@dataclass(frozen=True)
class CustomAutoConfig:
    """Validated custom-auto configuration."""

    confirmation_delay_seconds: int
    thresholds: tuple[int, int, int, int]
    down_delays: tuple[int, int, int, int]

    @classmethod
    def from_options(
        cls,
        options: Mapping[str, Any],
        defaults: Mapping[str, int] = CUSTOM_AUTO_DEFAULTS,
    ) -> "CustomAutoConfig":
        """Read options, falling back safely for missing or malformed entries."""

        try:
            values = parse_custom_auto_values(options, defaults)
            validate_custom_auto_values(values)
        except ValueError:
            values = dict(defaults)
        return cls(
            confirmation_delay_seconds=values[CONF_CUSTOM_AUTO_CONFIRMATION_DELAY],
            thresholds=tuple(values[key] for key in THRESHOLD_KEYS),
            down_delays=tuple(values[key] for key in DOWN_DELAY_KEYS),
        )

    def as_options(self) -> dict[str, int]:
        """Return the configuration in config-entry option form."""

        values = {CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: self.confirmation_delay_seconds}
        values.update(dict(zip(THRESHOLD_KEYS, self.thresholds, strict=True)))
        values.update(dict(zip(DOWN_DELAY_KEYS, self.down_delays, strict=True)))
        return values


def parse_custom_auto_values(
    values: Mapping[str, Any],
    defaults: Mapping[str, int] = CUSTOM_AUTO_DEFAULTS,
) -> dict[str, int]:
    """Parse bounded integer rule values, applying defaults for missing fields."""

    parsed: dict[str, int] = {}
    for key in CUSTOM_AUTO_OPTION_KEYS:
        value = values.get(key, defaults[key])
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
    """Validate Custom Auto threshold ordering."""

    thresholds = tuple(values[key] for key in THRESHOLD_KEYS)
    if not all(left < right for left, right in zip(thresholds, thresholds[1:])):
        raise ValueError("thresholds_not_ascending")
