"""Internal notification routing contracts."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ..govee_ble_air_purifier_protocol import ProtocolError


@dataclass(slots=True)
class TransactionNotificationRoute:
    """Connection-level callbacks owned by one active transaction."""

    handle_frame: Callable[[bytes], bool]
    handle_error: Callable[[ProtocolError], None]
    handle_stale_handshake: Callable[[int], None]
    handle_nonmatching: Callable[[], None]
