"""Constants for the Govee BLE Air Purifier integration."""

from __future__ import annotations

DOMAIN = "govee_ble_air_purifier"
MANUFACTURER = "Govee"

CONF_ADDRESS = "address"
CONF_DISCOVERED_DEVICE = "discovered_device"
CONF_NAME = "name"
CONF_POLLING_INTERVAL = "polling_interval"
CONF_PROFILE = "profile"
# Retained only to remove the former settings toggle from existing options.
LEGACY_CONF_USE_CUSTOM_AUTO = "use_custom_auto"
CONF_CUSTOM_AUTO_UP_40 = "custom_auto_up_40"
CONF_CUSTOM_AUTO_UP_60 = "custom_auto_up_60"
CONF_CUSTOM_AUTO_UP_80 = "custom_auto_up_80"
CONF_CUSTOM_AUTO_UP_100 = "custom_auto_up_100"
CONF_CUSTOM_AUTO_DOWN_20 = "custom_auto_down_20"
CONF_CUSTOM_AUTO_DOWN_40 = "custom_auto_down_40"
CONF_CUSTOM_AUTO_DOWN_60 = "custom_auto_down_60"
CONF_CUSTOM_AUTO_DOWN_80 = "custom_auto_down_80"
CONF_CUSTOM_AUTO_DELAY_20 = "custom_auto_delay_20"
CONF_CUSTOM_AUTO_DELAY_40 = "custom_auto_delay_40"
CONF_CUSTOM_AUTO_DELAY_60 = "custom_auto_delay_60"
CONF_CUSTOM_AUTO_DELAY_80 = "custom_auto_delay_80"

DEFAULT_POLLING_INTERVAL_SECONDS = 15
MIN_POLLING_INTERVAL_SECONDS = 5
MAX_POLLING_INTERVAL_SECONDS = 300

PLATFORMS = ["fan", "sensor", "switch"]
