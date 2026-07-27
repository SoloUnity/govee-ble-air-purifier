#!/usr/bin/env python3
"""Manually scan for nearby Bluetooth Low Energy advertisements."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Mapping
from typing import Any


def _positive_seconds(value: str) -> float:
    """Parse a positive scan duration."""

    duration = float(value)
    if duration <= 0:
        raise argparse.ArgumentTypeError("duration must be greater than zero")
    return duration


def _format_bytes(value: bytes) -> str:
    """Format advertisement bytes as space-separated hexadecimal."""

    return value.hex(" ") or "<empty>"


def _print_bytes_mapping(
    label: str, values: Mapping[Any, bytes], *, integer_keys: bool = False
) -> None:
    """Print manufacturer or service data when present."""

    if not values:
        return
    print(f"  {label}:")
    for key, value in sorted(values.items(), key=lambda item: str(item[0])):
        rendered_key = f"0x{key:04x}" if integer_keys else str(key)
        print(f"    {rendered_key}: {_format_bytes(value)}")


async def _scan(duration: float) -> dict[str, tuple[Any, Any]]:
    """Return all BLE devices observed during one scan window."""

    try:
        from bleak import BleakScanner
    except ModuleNotFoundError as err:
        raise SystemExit(
            "This manual test requires Bleak. Install it with: python -m pip install bleak"
        ) from err

    return await BleakScanner.discover(timeout=duration, return_adv=True)


def _print_results(discovered: dict[str, tuple[Any, Any]]) -> None:
    """Print discovered devices in descending signal-strength order."""

    devices = sorted(
        discovered.values(),
        key=lambda item: (
            -item[1].rssi,
            (item[1].local_name or item[0].name or "").casefold(),
            item[0].address,
        ),
    )
    print(f"Found {len(devices)} BLE device(s).")

    for index, (device, advertisement) in enumerate(devices, start=1):
        name = advertisement.local_name or device.name or "<unknown>"
        print(f"\n[{index}] {name}")
        print(f"  address: {device.address}")
        print(f"  RSSI: {advertisement.rssi} dBm")
        if advertisement.local_name:
            print(f"  local name: {advertisement.local_name}")
        if advertisement.tx_power is not None:
            print(f"  TX power: {advertisement.tx_power} dBm")
        if advertisement.service_uuids:
            print("  service UUIDs:")
            for service_uuid in sorted(advertisement.service_uuids):
                print(f"    {service_uuid}")
        _print_bytes_mapping(
            "manufacturer data", advertisement.manufacturer_data, integer_keys=True
        )
        _print_bytes_mapping("service data", advertisement.service_data)


def main() -> None:
    """Run the manual BLE scan."""

    parser = argparse.ArgumentParser(
        description="Passively scan for all nearby BLE advertisements."
    )
    parser.add_argument(
        "--duration",
        type=_positive_seconds,
        default=15.0,
        help="scan duration in seconds (default: 15)",
    )
    args = parser.parse_args()

    try:
        discovered = asyncio.run(_scan(args.duration))
    except Exception as err:
        raise SystemExit(f"Bluetooth scan failed: {err}") from err
    _print_results(discovered)


if __name__ == "__main__":
    main()
