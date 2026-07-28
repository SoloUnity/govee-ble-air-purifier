"""Shared Bluetooth support for the Govee BLE integration."""


class GoveeBleClientError(Exception):
    """Raised when BLE communication fails."""


class GoveeBleDisconnectedError(GoveeBleClientError):
    """Raised when an established BLE link drops during an operation."""
