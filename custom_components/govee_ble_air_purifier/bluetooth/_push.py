"""Decode, publish, and account for unsolicited purifier notifications."""

from __future__ import annotations

from collections.abc import Callable
import logging
import time

from ..govee_ble_air_purifier_protocol import (
    ModelProfile,
    PurifierPushUpdate,
    decode_mode_push,
    decode_night_light_power_brightness_push,
    is_mode_push,
    ProtocolError,
)

_LOGGER = logging.getLogger(__name__)


class PushDispatcher:
    """Own profile-aware push decoding, delivery, and diagnostics."""

    def __init__(self, profile: ModelProfile, log_label: str) -> None:
        self._profile = profile
        self._log_label = log_label
        self._callback: Callable[[PurifierPushUpdate], None] | None = None
        self._counts = {"power": 0, "fan_mode": 0, "night_light": 0}
        self._ignored_count = 0
        self._last_push_at: float | None = None

    def set_callback(
        self, callback: Callable[[PurifierPushUpdate], None] | None
    ) -> None:
        """Register or detach the non-blocking push callback."""

        self._callback = callback

    def diagnostics(self) -> dict[str, object]:
        """Return non-sensitive push-delivery diagnostics."""

        return {
            "push_counts": dict(self._counts),
            "ignored_push_count": self._ignored_count,
            "last_push_age_seconds": (
                max(0.0, time.monotonic() - self._last_push_at)
                if self._last_push_at is not None
                else None
            ),
        }

    def dispatch(self, frame: bytes) -> bool:
        """Decode and publish one profile-enabled unsolicited frame."""

        push = self._profile.push_notifications
        candidate = False
        update: PurifierPushUpdate | None = None
        push_kind: str | None = None

        if self._profile.is_power_state_response(frame):
            candidate = True
            if push is not None and push.power_state:
                update = PurifierPushUpdate(
                    is_on=self._profile.decode_power_state(frame)
                )
                push_kind = "power"
        elif is_mode_push(frame):
            candidate = True
            if push is not None and push.fan_mode:
                try:
                    mode = decode_mode_push(frame, self._profile.fan_mode_commands)
                except ProtocolError:
                    mode = None
                if mode is not None:
                    update = PurifierPushUpdate(fan_mode=mode)
                    push_kind = "fan_mode"
        elif len(frame) == 20 and frame[:3] == bytes.fromhex("ee 1b 01"):
            candidate = True
            if push is not None and push.night_light_power_brightness:
                try:
                    night_light = decode_night_light_power_brightness_push(frame)
                except ProtocolError:
                    night_light = None
                if night_light is not None:
                    update = PurifierPushUpdate(night_light=night_light)
                    push_kind = "night_light"

        if update is None:
            if candidate:
                self._ignored_count += 1
                _LOGGER.debug(
                    "%s ignored unsupported or profile-mismatched purifier push",
                    self._log_label,
                )
            return candidate

        assert push_kind is not None
        self._counts[push_kind] += 1
        self._last_push_at = time.monotonic()
        _LOGGER.debug(
            "%s received %s purifier push (count: %d)",
            self._log_label,
            push_kind,
            self._counts[push_kind],
        )
        callback = self._callback
        if callback is not None:
            try:
                callback(update)
            except Exception:
                _LOGGER.exception(
                    "%s failed to schedule purifier push update", self._log_label
                )
        return True
