"""Constants for the Govee BLE Air Purifier integration."""

from __future__ import annotations

from .govee_ble_air_purifier_protocol import (
    MAX_POLLING_INTERVAL_SECONDS as MAX_POLLING_INTERVAL_SECONDS,
)
from .govee_ble_air_purifier_protocol import (
    MIN_POLLING_INTERVAL_SECONDS as MIN_POLLING_INTERVAL_SECONDS,
)

DOMAIN = "govee_ble_air_purifier"
MANUFACTURER = "Govee"

CONF_ADDRESS = "address"
CONF_DISCOVERED_DEVICE = "discovered_device"
CONF_NAME = "name"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_SHARE_BLUETOOTH_CONNECTION = "share_bluetooth_connection"
CONF_PROFILE = "profile"
# Retained only to remove the former settings toggle from existing options.
LEGACY_CONF_USE_CUSTOM_AUTO = "use_custom_auto"
CONF_CUSTOM_AUTO_CONFIRMATION_DELAY = "custom_auto_confirmation_delay"
CONF_CUSTOM_AUTO_THRESHOLD_40 = "custom_auto_threshold_40"
CONF_CUSTOM_AUTO_THRESHOLD_60 = "custom_auto_threshold_60"
CONF_CUSTOM_AUTO_THRESHOLD_80 = "custom_auto_threshold_80"
CONF_CUSTOM_AUTO_THRESHOLD_100 = "custom_auto_threshold_100"
CONF_CUSTOM_AUTO_DELAY_20 = "custom_auto_delay_20"
CONF_CUSTOM_AUTO_DELAY_40 = "custom_auto_delay_40"
CONF_CUSTOM_AUTO_DELAY_60 = "custom_auto_delay_60"
CONF_CUSTOM_AUTO_DELAY_80 = "custom_auto_delay_80"

PLATFORMS = ["fan", "sensor", "switch", "light"]
