"""Contracts for values persisted by Home Assistant across releases."""

from custom_components.govee_ble_air_purifier.auto_resume import (
    ATTR_AUTO_RESUME_CUSTOM_SPEED,
    ATTR_AUTO_RESUME_MODE,
    ATTR_AUTO_RESUME_SUSPENDED,
    AUTO_MODE_CUSTOM,
    AUTO_MODE_HARDWARE,
)
from custom_components.govee_ble_air_purifier.const import (
    CONF_ADDRESS,
    CONF_CUSTOM_AUTO_CONFIRMATION_DELAY,
    CONF_CUSTOM_AUTO_DELAY_20,
    CONF_CUSTOM_AUTO_DELAY_40,
    CONF_CUSTOM_AUTO_DELAY_60,
    CONF_CUSTOM_AUTO_DELAY_80,
    CONF_CUSTOM_AUTO_THRESHOLD_100,
    CONF_CUSTOM_AUTO_THRESHOLD_40,
    CONF_CUSTOM_AUTO_THRESHOLD_60,
    CONF_CUSTOM_AUTO_THRESHOLD_80,
    CONF_NAME,
    CONF_POLLING_INTERVAL,
    CONF_PROFILE,
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
        "custom_auto_confirmation_delay",
        "custom_auto_threshold_40",
        "custom_auto_threshold_60",
        "custom_auto_threshold_80",
        "custom_auto_threshold_100",
        "custom_auto_delay_20",
        "custom_auto_delay_40",
        "custom_auto_delay_60",
        "custom_auto_delay_80",
    )


def test_persisted_defaults_and_profile_fallback_are_stable() -> None:
    assert CUSTOM_AUTO_DEFAULTS == {
        CONF_CUSTOM_AUTO_CONFIRMATION_DELAY: 3,
        CONF_CUSTOM_AUTO_THRESHOLD_40: 3,
        CONF_CUSTOM_AUTO_THRESHOLD_60: 5,
        CONF_CUSTOM_AUTO_THRESHOLD_80: 9,
        CONF_CUSTOM_AUTO_THRESHOLD_100: 15,
        CONF_CUSTOM_AUTO_DELAY_20: 7,
        CONF_CUSTOM_AUTO_DELAY_40: 5,
        CONF_CUSTOM_AUTO_DELAY_60: 5,
        CONF_CUSTOM_AUTO_DELAY_80: 5,
    }
    assert H7124_PROFILE.key == "h7124"
    assert H7124_PROFILE.polling_interval_seconds == 10
    assert H7124_PROFILE.night_light.poll_timeout_seconds == 0
    assert H7124_PROFILE.custom_auto_thresholds == (3, 5, 9, 15)
    assert get_profile(None) is H7124_PROFILE
    assert get_profile("h7124") is H7124_PROFILE
    fallback = get_profile("h712c")
    assert fallback.key == "h712c"
    assert fallback.model == "H712C"
    assert fallback.fan_mode_commands == H7124_PROFILE.fan_mode_commands
    assert fallback.polling_interval_seconds == 10
    assert fallback.night_light is None


def test_active_platform_contract_is_stable() -> None:
    assert PLATFORMS == ["fan", "sensor", "switch", "light"]


def test_auto_resume_restore_values_are_stable() -> None:
    assert (
        ATTR_AUTO_RESUME_MODE,
        ATTR_AUTO_RESUME_SUSPENDED,
        ATTR_AUTO_RESUME_CUSTOM_SPEED,
    ) == (
        "auto_resume_mode",
        "auto_resume_suspended",
        "auto_resume_custom_speed",
    )
    assert (AUTO_MODE_HARDWARE, AUTO_MODE_CUSTOM) == (
        "hardware_auto",
        "custom_auto",
    )
