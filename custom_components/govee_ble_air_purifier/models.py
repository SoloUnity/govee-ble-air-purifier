"""Models for Govee BLE air purifier state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GoveeAirPurifierState:
    """Partial purifier state decoded from BLE status frames."""

    is_on: bool | None = None
    pm25: int | None = None
    filter_life: int | None = None
    fan_mode: str | None = None
