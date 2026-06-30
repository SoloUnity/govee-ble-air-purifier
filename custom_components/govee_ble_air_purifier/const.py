"""Constants for the Govee BLE Air Purifier integration."""

from __future__ import annotations

DOMAIN = "govee_ble_air_purifier"
MANUFACTURER = "Govee"

CONF_ADDRESS = "address"
CONF_DISCOVERED_DEVICE = "discovered_device"
CONF_NAME = "name"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_PROFILE = "profile"

DEFAULT_POLLING_INTERVAL_SECONDS = 45
MIN_POLLING_INTERVAL_SECONDS = 5
MAX_POLLING_INTERVAL_SECONDS = 300

PLATFORMS = ["switch", "select", "sensor"]
