"""Compatibility exports for the reusable model-profile registry.

New code should import from :mod:`govee_ble_air_purifier_protocol` when using
the independently built package. Existing custom-integration imports remain
supported by this facade.
"""

from .govee_ble_air_purifier_protocol.profiles import (
    DEFAULT_PROFILE_KEY,
    H7124_PROFILE,
    H7124_PROFILE_KEY,
    MAX_POLLING_INTERVAL_SECONDS,
    MIN_POLLING_INTERVAL_SECONDS,
    MODEL_PROFILE_SCHEMA_PATH,
    PROFILE_DIRECTORY,
    PROFILE_SCHEMA_VERSION,
    PROFILES,
    PROFILES_BY_KEY,
    EncryptionMode,
    ModelProfile,
    ModelSupportStatus,
    NightLightPollingCadence,
    NightLightPollingProfile,
    NightLightPollingRequestOrder,
    NightLightProfile,
    PushNotificationProfile,
    canonicalize_ble_address,
    fan_mode_labels,
    get_profile,
    match_profile,
    model_from_ble_name,
    normalize_ble_address,
    normalize_ble_name,
)
from .govee_ble_air_purifier_protocol.profiles import (
    _build_profile as _build_profile,
)
from .govee_ble_air_purifier_protocol.profiles import (
    _load_profile_definitions as _load_profile_definitions,
)
from .govee_ble_air_purifier_protocol.profiles import (
    _parse_profile_definition as _parse_profile_definition,
)

__all__ = [
    "DEFAULT_PROFILE_KEY",
    "EncryptionMode",
    "H7124_PROFILE",
    "H7124_PROFILE_KEY",
    "MAX_POLLING_INTERVAL_SECONDS",
    "MIN_POLLING_INTERVAL_SECONDS",
    "MODEL_PROFILE_SCHEMA_PATH",
    "ModelProfile",
    "ModelSupportStatus",
    "NightLightPollingCadence",
    "NightLightPollingProfile",
    "NightLightPollingRequestOrder",
    "NightLightProfile",
    "PROFILE_DIRECTORY",
    "PROFILE_SCHEMA_VERSION",
    "PROFILES",
    "PROFILES_BY_KEY",
    "PushNotificationProfile",
    "canonicalize_ble_address",
    "fan_mode_labels",
    "get_profile",
    "match_profile",
    "model_from_ble_name",
    "normalize_ble_address",
    "normalize_ble_name",
]
