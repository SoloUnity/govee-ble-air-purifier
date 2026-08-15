"""Focused tests for stateful helpers extracted from the BLE client."""

from custom_components.govee_ble_air_purifier.bluetooth._night_light_polling import (
    NightLightPollingTracker,
)
from custom_components.govee_ble_air_purifier.bluetooth._push import PushDispatcher
from custom_components.govee_ble_air_purifier.bluetooth.framing import build_frame
from custom_components.govee_ble_air_purifier.models import PurifierPushUpdate
from custom_components.govee_ble_air_purifier.profiles import H7124_PROFILE

NIGHT_LIGHT = H7124_PROFILE.night_light
assert NIGHT_LIGHT is not None


def test_night_light_polling_tracker_reserves_and_backs_off() -> None:
    """A missed periodic reconciliation is delayed without blocking core state."""

    tracker = NightLightPollingTracker(NIGHT_LIGHT)

    assert tracker.claim(100.0) is True
    assert tracker.claim(101.0) is False

    tracker.record_result((None, None), 102.0)

    diagnostics = tracker.diagnostics(102.0)
    assert diagnostics is not None
    assert diagnostics["attempt_count"] == 1
    assert diagnostics["missed_count"] == 1
    assert diagnostics["consecutive_failures"] == 1
    assert diagnostics["next_attempt_in_seconds"] == 600


def test_night_light_polling_tracker_is_disabled_without_capability() -> None:
    """Profiles without a night light never reserve optional requests."""

    tracker = NightLightPollingTracker(None)

    assert tracker.claim(100.0) is False
    assert tracker.diagnostics(100.0) is None


def test_push_dispatcher_decodes_and_accounts_for_enabled_push() -> None:
    """The extracted dispatcher owns delivery and push diagnostics."""

    updates: list[PurifierPushUpdate] = []
    dispatcher = PushDispatcher(H7124_PROFILE, "H7124 [test]")
    dispatcher.set_callback(updates.append)

    assert dispatcher.dispatch(
        build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
    )

    assert updates == [PurifierPushUpdate(is_on=False)]
    assert dispatcher.diagnostics()["push_counts"] == {
        "power": 1,
        "fan_mode": 0,
        "night_light": 0,
    }
