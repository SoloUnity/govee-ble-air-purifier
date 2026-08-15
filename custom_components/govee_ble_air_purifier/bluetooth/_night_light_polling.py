"""Night-light reconciliation scheduling and health accounting."""

from __future__ import annotations

from dataclasses import dataclass

from ..govee_ble_air_purifier_protocol import (
    NightLightPollingCadence,
    NightLightProfile,
)


@dataclass(slots=True)
class NightLightPollingTracker:
    """Track when optional night-light telemetry is due and its health."""

    profile: NightLightProfile | None
    next_at: float = 0.0
    last_at: float | None = None
    last_success_at: float | None = None
    attempt_count: int = 0
    success_count: int = 0
    partial_count: int = 0
    missed_count: int = 0
    consecutive_failures: int = 0

    def claim(self, now: float) -> bool:
        """Reserve one due reconciliation before any transaction await."""

        if self.profile is None:
            return False
        polling = self.profile.polling
        if (
            polling.cadence is NightLightPollingCadence.PERIODIC
            and now < self.next_at
        ):
            return False
        if polling.cadence is NightLightPollingCadence.PERIODIC:
            self.next_at = now + polling.interval_seconds
        return True

    def release_claim(self) -> None:
        """Make a periodic reconciliation due again after core polling failed."""

        if (
            self.profile is not None
            and self.profile.polling.cadence is NightLightPollingCadence.PERIODIC
        ):
            self.next_at = 0.0

    def record_result(
        self, frames: tuple[bytes | None, bytes | None], now: float
    ) -> None:
        """Record reconciliation health and schedule profile-defined backoff."""

        if self.profile is None:
            return
        received_count = sum(frame is not None for frame in frames)
        self.attempt_count += 1
        self.last_at = now
        if received_count == len(frames):
            self.success_count += 1
            self.consecutive_failures = 0
            self.last_success_at = now
        else:
            if received_count:
                self.partial_count += 1
            else:
                self.missed_count += 1
            self.consecutive_failures += 1

        polling = self.profile.polling
        if polling.cadence is not NightLightPollingCadence.PERIODIC:
            return
        interval = polling.interval_seconds
        if received_count != len(frames):
            interval = min(
                polling.max_backoff_seconds,
                interval * (2 ** min(self.consecutive_failures, 16)),
            )
        self.next_at = now + interval

    def diagnostics(self, now: float) -> dict[str, str | float | int | None] | None:
        """Return non-sensitive reconciliation diagnostics."""

        if self.profile is None:
            return None
        polling = self.profile.polling
        return {
            "cadence": polling.cadence.value,
            "interval_seconds": polling.interval_seconds,
            "request_order": polling.request_order.value,
            "attempt_count": self.attempt_count,
            "success_count": self.success_count,
            "partial_count": self.partial_count,
            "missed_count": self.missed_count,
            "consecutive_failures": self.consecutive_failures,
            "last_attempt_age_seconds": (
                max(0.0, now - self.last_at) if self.last_at is not None else None
            ),
            "last_success_age_seconds": (
                max(0.0, now - self.last_success_at)
                if self.last_success_at is not None
                else None
            ),
            "next_attempt_in_seconds": (
                max(0.0, self.next_at - now)
                if polling.cadence is NightLightPollingCadence.PERIODIC
                else 0.0
            ),
        }
