"""Pure setup helpers for config and options flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .const import (
    CONF_POLLING_INTERVAL,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
)
from .profiles import (
    canonicalize_ble_address,
    match_profile,
    normalize_ble_address,
)

MANUAL_DEVICE_VALUE = "__manual__"


@dataclass(frozen=True)
class DiscoveredDeviceOption:
    """Selectable representation of a discovered purifier."""

    value: str
    label: str
    name: str
    profile_key: str


def build_discovered_device_options(
    service_infos: list[Any] | tuple[Any, ...],
) -> tuple[DiscoveredDeviceOption, ...]:
    """Return supported BLE discoveries sorted by strongest signal first."""

    by_address: dict[str, Any] = {}
    for service_info in service_infos:
        profile = match_profile(getattr(service_info, "name", None))
        if profile is None:
            continue
        address = getattr(service_info, "address", "")
        try:
            canonicalize_ble_address(address)
        except ValueError:
            continue
        normalized = normalize_ble_address(address)
        current = by_address.get(normalized)
        if current is None or _rssi_sort_value(service_info) > _rssi_sort_value(
            current
        ):
            by_address[normalized] = service_info

    sorted_infos = sorted(by_address.values(), key=_rssi_sort_value, reverse=True)
    options: list[DiscoveredDeviceOption] = []
    for service_info in sorted_infos:
        name = getattr(service_info, "name", None) or "Govee Air Purifier"
        profile = match_profile(name)
        if profile is None:
            continue
        address = getattr(service_info, "address", "")
        options.append(
            DiscoveredDeviceOption(
                value=address,
                label=_format_device_label(service_info, name, address),
                name=name,
                profile_key=profile.key,
            )
        )
    return tuple(options)


def validate_polling_interval_seconds(value: object) -> int:
    """Validate and normalize a polling interval in seconds."""

    try:
        seconds = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as err:
        raise ValueError("Polling interval must be a number of seconds") from err
    if not MIN_POLLING_INTERVAL_SECONDS <= seconds <= MAX_POLLING_INTERVAL_SECONDS:
        raise ValueError("Polling interval is outside the supported range")
    return seconds


def polling_interval_from_options(options: Mapping[str, Any]) -> int:
    """Read a stored polling interval, falling back to the default if invalid."""

    try:
        return validate_polling_interval_seconds(
            options.get(CONF_POLLING_INTERVAL, DEFAULT_POLLING_INTERVAL_SECONDS)
        )
    except ValueError:
        return DEFAULT_POLLING_INTERVAL_SECONDS


def _format_device_label(service_info: Any, name: str, address: str) -> str:
    label = f"{name} ({address}) - {_signal_label(getattr(service_info, 'rssi', None))}"
    source = getattr(service_info, "source", None)
    if source:
        label = f"{label} via {source}"
    return label


def _signal_label(rssi: int | None) -> str:
    if rssi is None:
        return "Signal unknown"
    if rssi >= -55:
        return f"Very close signal ({rssi} dBm)"
    if rssi >= -70:
        return f"Nearby signal ({rssi} dBm)"
    if rssi >= -85:
        return f"Weak signal ({rssi} dBm)"
    return f"Very weak signal ({rssi} dBm)"


def _rssi_sort_value(service_info: Any) -> int:
    rssi = getattr(service_info, "rssi", None)
    return rssi if isinstance(rssi, int) else -999
