"""Data models for Govee BLE air purifiers."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DecodedStatus:
    """PM2.5 and filter-life values decoded from an aa19 status frame."""

    pm25: int | None = None
    filter_life: int | None = None


@dataclass(frozen=True)
class NightLightState:
    """Application-facing snapshot of night-light state."""

    is_on: bool | None = None
    brightness_percent: int | None = None
    rgb_color: tuple[int, int, int] | None = None


@dataclass(frozen=True)
class PurifierState:
    """Application-facing snapshot of purifier state."""

    is_on: bool | None = None
    pm25: int | None = None
    filter_life: int | None = None
    fan_mode: str | None = None
    night_light: NightLightState | None = None


@dataclass(frozen=True)
class PurifierPushUpdate:
    """Partial state reported asynchronously by a connected purifier."""

    is_on: bool | None = None
    fan_mode: str | None = None
    night_light: NightLightState | None = None
