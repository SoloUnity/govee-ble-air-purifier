"""Contracts for values persisted by Home Assistant across releases."""

from custom_components.govee_ble_air_purifier.const import (
    CONF_ADDRESS,
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
    CONF_NAME,
    CONF_POLLING_INTERVAL,
    CONF_PROFILE,
    DEFAULT_POLLING_INTERVAL_SECONDS,
    LEGACY_CONF_USE_CUSTOM_AUTO,
    PLATFORMS,
)
from custom_components.govee_ble_air_purifier.custom_auto.config import (
    CUSTOM_AUTO_DEFAULTS,
    CUSTOM_AUTO_OPTION_KEYS,
)
from custom_components.govee_ble_air_purifier.profiles import (
    H7124_PROFILE,
    get_profile,
)


def test_config_entry_and_option_storage_keys_are_stable() -> None:
    assert (CONF_ADDRESS, CONF_NAME, CONF_PROFILE) == ("address", "name", "profile")
    assert LEGACY_CONF_USE_CUSTOM_AUTO == "use_custom_auto"
    assert (CONF_POLLING_INTERVAL, *CUSTOM_AUTO_OPTION_KEYS) == (
        "polling_interval",
        "custom_auto_up_40",
        "custom_auto_up_60",
        "custom_auto_up_80",
        "custom_auto_up_100",
        "custom_auto_down_20",
        "custom_auto_down_40",
        "custom_auto_down_60",
        "custom_auto_down_80",
        "custom_auto_delay_20",
        "custom_auto_delay_40",
        "custom_auto_delay_60",
        "custom_auto_delay_80",
    )


def test_persisted_defaults_and_profile_fallback_are_stable() -> None:
    assert DEFAULT_POLLING_INTERVAL_SECONDS == 10
    assert CUSTOM_AUTO_DEFAULTS == {
        CONF_CUSTOM_AUTO_UP_40: 3,
        CONF_CUSTOM_AUTO_UP_60: 5,
        CONF_CUSTOM_AUTO_UP_80: 9,
        CONF_CUSTOM_AUTO_UP_100: 15,
        CONF_CUSTOM_AUTO_DOWN_20: 3,
        CONF_CUSTOM_AUTO_DOWN_40: 5,
        CONF_CUSTOM_AUTO_DOWN_60: 9,
        CONF_CUSTOM_AUTO_DOWN_80: 14,
        CONF_CUSTOM_AUTO_DELAY_20: 7,
        CONF_CUSTOM_AUTO_DELAY_40: 5,
        CONF_CUSTOM_AUTO_DELAY_60: 5,
        CONF_CUSTOM_AUTO_DELAY_80: 5,
    }
    assert H7124_PROFILE.key == "h7124"
    assert get_profile(None) is H7124_PROFILE
    assert get_profile("h7124") is H7124_PROFILE


def test_active_platform_contract_is_stable() -> None:
    assert PLATFORMS == ["fan", "sensor", "switch"]
