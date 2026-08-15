"""Connection-generation and encrypted-session ownership for the BLE client."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from ..govee_ble_air_purifier_protocol import (
    EncryptionMode,
    ProtocolError,
    decrypt_frame,
    encrypt_frame,
)
from . import GoveeBleClientError, GoveeBleDisconnectedError
from ._notifications import TransactionNotificationRoute

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class DetachedConnection:
    """State detached atomically before best-effort transport cleanup."""

    client: Any | None
    connection_age: float | None
    encrypted_session_age: float | None
    notifications_active: bool


class GoveeBleSession:
    """Own one cached BLE connection and its connection-scoped state.

    Transport establishment and protocol negotiation remain policy decisions of
    ``GoveeBleClient``. This collaborator owns the resulting identity and makes
    generation checks, disconnect signalling, encryption, and invalidation
    atomic from the client's point of view.
    """

    def __init__(
        self,
        cancel_and_observe: Callable[[asyncio.Future[Any]], None],
    ) -> None:
        self._cancel_and_observe = cancel_and_observe
        self._client: Any | None = None
        self._disconnect_signal: asyncio.Event | None = None
        self._session_key: bytes | None = None
        self._connected_at: float | None = None
        self._session_started_at: float | None = None
        self._notifications_client: Any | None = None
        self._transaction_route: TransactionNotificationRoute | None = None
        self._generation = 0
        self._unexpected_disconnect_revision = 0
        self._abandoned_operations: set[asyncio.Future[Any]] = set()

    @property
    def client(self) -> Any | None:
        """Return the exact cached transport client, if any."""

        return self._client

    @property
    def generation(self) -> int:
        """Return the revision used to reject stale notification callbacks."""

        return self._generation

    @property
    def unexpected_disconnect_revision(self) -> int:
        """Return the count of accepted unexpected-disconnect callbacks."""

        return self._unexpected_disconnect_revision

    @property
    def session_key(self) -> bytes | None:
        """Return the active encrypted-session key for compatibility tests."""

        return self._session_key

    @property
    def connected_at(self) -> float | None:
        """Return the active transport start timestamp."""

        return self._connected_at

    @property
    def session_started_at(self) -> float | None:
        """Return the encrypted-session start timestamp, if applicable."""

        return self._session_started_at

    @property
    def disconnect_signal(self) -> asyncio.Event | None:
        """Return the active connection's disconnect signal."""

        return self._disconnect_signal

    @property
    def notifications_client(self) -> Any | None:
        """Return the client that owns application notification subscription."""

        return self._notifications_client

    @property
    def transaction_route(self) -> TransactionNotificationRoute | None:
        """Return the response route temporarily bound to this session."""

        return self._transaction_route

    @property
    def abandoned_operations(self) -> set[asyncio.Future[Any]]:
        """Expose the live quarantine set for diagnostics and compatibility."""

        return self._abandoned_operations

    def is_current(self, client: Any, generation: int | None = None) -> bool:
        """Return whether an identity, and optionally generation, is current."""

        return self._client is client and (
            generation is None or self._generation == generation
        )

    def has_connected_client(self) -> bool:
        """Return whether the cached client still reports an active transport."""

        return self._client is not None and bool(self._client.is_connected)

    def disconnect_signal_for(self, client: Any) -> asyncio.Event | None:
        """Return a disconnect signal only for the exact cached identity."""

        if self._client is client:
            return self._disconnect_signal
        return None

    def reset_for_connection_attempt(self) -> None:
        """Invalidate stale state before establishing a replacement transport."""

        self._clear()

    def attach(self, client: Any, connected_at: float) -> None:
        """Attach a newly established transport to the current generation."""

        if self._client is not None:
            raise GoveeBleClientError("Cannot replace an active BLE connection")
        self._client = client
        self._disconnect_signal = asyncio.Event()
        self._connected_at = connected_at

    def activate_encryption(
        self, client: Any, session_key: bytes, started_at: float
    ) -> None:
        """Bind a negotiated key to the exact active transport."""

        if not self.is_current(client) or not client.is_connected:
            raise GoveeBleDisconnectedError(
                "Purifier disconnected during encrypted-session setup"
            )
        self._session_key = session_key
        self._session_started_at = started_at

    def encode(self, frame: bytes, encryption: EncryptionMode) -> bytes:
        """Encode a plaintext application frame for the active session."""

        if encryption is EncryptionMode.NONE:
            return frame
        if self._session_key is None:
            raise GoveeBleClientError("Encrypted purifier session is unavailable")
        return encrypt_frame(frame, self._session_key)

    def decode(self, frame: bytes, encryption: EncryptionMode) -> bytes:
        """Decode one wire notification using the active session."""

        if encryption is EncryptionMode.NONE:
            return frame
        if self._session_key is None:
            raise ProtocolError("Encrypted purifier session is unavailable")
        return decrypt_frame(frame, self._session_key)

    def notifications_active_for(self, client: Any) -> bool:
        """Return whether application notifications belong to this client."""

        return self._notifications_client is client

    def mark_notifications_active(self, client: Any, generation: int) -> None:
        """Record notification ownership after verifying connection identity."""

        if not self.is_current(client, generation) or not client.is_connected:
            raise GoveeBleDisconnectedError(
                "Purifier disconnected while starting notifications"
            )
        self._notifications_client = client

    def release_notifications(self, client: Any) -> bool:
        """Invalidate notification ownership for an exact client."""

        if self._notifications_client is not client:
            return False
        self._notifications_client = None
        return True

    def invalidate_notifications(self, client: Any, generation: int) -> bool:
        """Reject a malformed listener and all callbacks from its generation."""

        if not self.is_current(client, generation):
            return False
        self._notifications_client = None
        self._generation += 1
        return True

    def bind_transaction_route(self, route: TransactionNotificationRoute) -> None:
        """Bind one route while its caller owns the serialized transaction lock."""

        if self._transaction_route is not None:
            raise GoveeBleClientError("A BLE transaction route is already active")
        self._transaction_route = route

    def unbind_transaction_route(self, route: TransactionNotificationRoute) -> None:
        """Clear a route only when it is still the caller's exact route."""

        if self._transaction_route is route:
            self._transaction_route = None

    def detach(
        self,
        now: float,
        *,
        expected_client: Any | None = None,
        signal_disconnect: bool = False,
        unexpected: bool = False,
    ) -> DetachedConnection | None:
        """Atomically forget a connection before bounded external cleanup."""

        if expected_client is not None and self._client is not expected_client:
            return None
        client = self._client
        connection_age = (
            max(0.0, now - self._connected_at)
            if self._connected_at is not None
            else None
        )
        session_age = (
            max(0.0, now - self._session_started_at)
            if self._session_started_at is not None
            else None
        )
        notifications_active = (
            client is not None and self._notifications_client is client
        )
        disconnect_signal = self._disconnect_signal
        self._clear()
        if signal_disconnect and disconnect_signal is not None:
            disconnect_signal.set()
        if unexpected:
            self._unexpected_disconnect_revision += 1
        return DetachedConnection(
            client=client,
            connection_age=connection_age,
            encrypted_session_age=session_age,
            notifications_active=notifications_active,
        )

    def reap_abandoned_operations(self) -> None:
        """Discard quarantine entries whose backend operations have exited."""

        for operation in tuple(self._abandoned_operations):
            if operation.done():
                self._abandoned_operations.discard(operation)

    @property
    def quarantined_operation_count(self) -> int:
        """Return the number of backend operations still ignoring cancellation."""

        return sum(not operation.done() for operation in self._abandoned_operations)

    def cancel_operation(self, operation: asyncio.Future[Any]) -> None:
        """Cancel and quarantine an operation until it actually exits."""

        if not operation.done() and operation not in self._abandoned_operations:
            self._abandoned_operations.add(operation)
            operation.add_done_callback(self._abandoned_operations.discard)
        self._cancel_and_observe(operation)

    async def async_wait(
        self,
        awaitable: Awaitable[_T],
        disconnect_signal: asyncio.Event | None,
        deadline: float,
    ) -> _T:
        """Wait until an operation completes, disconnects, or reaches deadline."""

        operation_task = asyncio.ensure_future(awaitable)
        if disconnect_signal is not None and disconnect_signal.is_set():
            self.cancel_operation(operation_task)
            raise GoveeBleDisconnectedError(
                "Purifier disconnected during BLE transaction"
            )
        if operation_task.done():
            return await operation_task

        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            self.cancel_operation(operation_task)
            raise TimeoutError

        disconnect_task = (
            asyncio.create_task(disconnect_signal.wait())
            if disconnect_signal is not None
            else None
        )
        waiters = (
            (operation_task, disconnect_task)
            if disconnect_task is not None
            else (operation_task,)
        )
        try:
            done, _pending = await asyncio.wait(
                waiters,
                timeout=remaining,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if disconnect_signal is not None and (
                disconnect_signal.is_set()
                or (disconnect_task is not None and disconnect_task in done)
            ):
                self.cancel_operation(operation_task)
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                )
            if operation_task in done:
                return await operation_task
            self.cancel_operation(operation_task)
            raise TimeoutError
        except asyncio.CancelledError:
            self.cancel_operation(operation_task)
            raise
        finally:
            if disconnect_task is not None:
                self._cancel_and_observe(disconnect_task)

    # These setters intentionally exist only for legacy tests and subclasses that
    # used the old private fields as monkeypatch seams. Runtime code uses the
    # cohesive methods above.
    def compat_set_client(self, client: Any | None) -> None:
        self._client = client

    def compat_set_disconnect_signal(self, signal: asyncio.Event | None) -> None:
        self._disconnect_signal = signal

    def compat_set_session_key(self, session_key: bytes | None) -> None:
        self._session_key = session_key

    def compat_set_connected_at(self, connected_at: float | None) -> None:
        self._connected_at = connected_at

    def compat_set_session_started_at(self, started_at: float | None) -> None:
        self._session_started_at = started_at

    def compat_set_notifications_client(self, client: Any | None) -> None:
        self._notifications_client = client

    def compat_set_generation(self, generation: int) -> None:
        self._generation = generation

    def compat_set_unexpected_disconnect_revision(self, revision: int) -> None:
        self._unexpected_disconnect_revision = revision

    def _clear(self) -> None:
        """Clear all state scoped to the current connection generation."""

        self._generation += 1
        self._client = None
        self._disconnect_signal = None
        self._session_key = None
        self._connected_at = None
        self._session_started_at = None
        self._notifications_client = None
        self._transaction_route = None
