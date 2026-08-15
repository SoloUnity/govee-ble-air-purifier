"""Bounded, read-only device validation for config-entry setup."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from .bluetooth import GoveeBleClientError
from .bluetooth.client import GoveeBleClient
from .govee_ble_air_purifier_protocol import ModelProfile

SETUP_PROBE_TIMEOUT = 65.0
SETUP_PROBE_CLEANUP_TIMEOUT = 10.0

_LOGGER = logging.getLogger(__name__)
_BACKGROUND_CLEANUPS: set[asyncio.Task[None]] = set()


class SetupProbeError(Exception):
    """A translated setup-probe failure."""

    def __init__(self, translation_key: str) -> None:
        super().__init__(translation_key)
        self.translation_key = translation_key


def _observe_cleanup(task: asyncio.Task[None]) -> None:
    """Observe a cleanup that outlived its config-flow deadline."""

    _BACKGROUND_CLEANUPS.discard(task)
    if task.cancelled():
        return
    try:
        task.exception()
    except (asyncio.CancelledError, asyncio.InvalidStateError):
        return


async def _async_close_probe_client(
    client: GoveeBleClient, cleanup_timeout: float
) -> bool:
    """Close a probe client or keep observing its bounded internal cleanup."""

    cleanup_task = asyncio.create_task(client.async_close())
    _BACKGROUND_CLEANUPS.add(cleanup_task)
    cleanup_task.add_done_callback(_observe_cleanup)
    try:
        await asyncio.wait_for(asyncio.shield(cleanup_task), cleanup_timeout)
    except (TimeoutError, asyncio.TimeoutError):
        _LOGGER.debug(
            "Setup probe cleanup exceeded its config-flow deadline; "
            "continuing in the background"
        )
        return False
    return True


async def async_probe_device(
    hass: Any,
    address: str,
    profile: ModelProfile,
    *,
    timeout: float = SETUP_PROBE_TIMEOUT,
    cleanup_timeout: float = SETUP_PROBE_CLEANUP_TIMEOUT,
) -> None:
    """Validate GATT and the profile protocol using read-only queries."""

    client = GoveeBleClient(
        hass,
        address,
        profile=profile,
        polling_interval_seconds=profile.polling_interval_seconds,
    )
    probe_error: SetupProbeError | None = None
    cancellation: asyncio.CancelledError | None = None
    try:
        await asyncio.wait_for(client.async_get_state(), timeout)
    except asyncio.CancelledError as err:
        cancellation = err
    except (TimeoutError, asyncio.TimeoutError):
        probe_error = SetupProbeError("probe_timeout")
    except GoveeBleClientError:
        _LOGGER.debug("Setup probe could not communicate with purifier", exc_info=True)
        probe_error = SetupProbeError("cannot_connect")
    except Exception:
        _LOGGER.debug("Setup probe rejected the purifier response", exc_info=True)
        probe_error = SetupProbeError("invalid_response")

    try:
        cleanup_finished = await _async_close_probe_client(client, cleanup_timeout)
        if not cleanup_finished and probe_error is None:
            probe_error = SetupProbeError("probe_cleanup_failed")
    except asyncio.CancelledError:
        # The shielded close remains tracked and observed even under repeated
        # cancellation. Preserve the config flow's cancellation semantics.
        if cancellation is None:
            raise
    except Exception:
        _LOGGER.debug("Setup probe cleanup failed", exc_info=True)
        if probe_error is None:
            probe_error = SetupProbeError("probe_cleanup_failed")

    if cancellation is not None:
        raise cancellation
    if probe_error is not None:
        raise probe_error
