"""Integration-wide scheduling for retained Govee BLE connections."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum
import logging
from typing import Any, Protocol

from . import GoveeBleClientError
from . import transport
from .transport import _async_wait_until

HANDSHAKE_TIMEOUT = 10.0
POLL_TIMEOUT = 5.0
DISCONNECT_TIMEOUT = 5.0
CONNECTION_IDLE_GRACE = 5.0
MAX_CONNECTION_IDLE_TIMEOUT = 30.0
CONNECTION_LEASE_TIMEOUT = (
    transport.CONNECTION_TIMEOUT + HANDSHAKE_TIMEOUT + POLL_TIMEOUT + DISCONNECT_TIMEOUT
)
MAX_PRIORITY_COMMAND_BURST = 3

_LOGGER = logging.getLogger(__name__)


class ConnectionLeaseClient(Protocol):
    """Minimal client surface required by the connection scheduler."""

    _log_label: str

    async def _async_release_for_connection_switch(self, deadline: float) -> None:
        """Release a retained connection before another client connects."""


def connection_idle_timeout_for_polling_interval(
    polling_interval_seconds: float,
) -> float:
    """Retain through the next poll or release after a short activity grace."""

    next_poll_timeout = polling_interval_seconds + CONNECTION_IDLE_GRACE
    if next_poll_timeout <= MAX_CONNECTION_IDLE_TIMEOUT:
        return next_poll_timeout
    return CONNECTION_IDLE_GRACE


class ConnectionLeasePriority(Enum):
    """Scheduling priority for integration-wide Bluetooth work."""

    COMMAND = "command"
    POLL = "poll"


@dataclass(slots=True)
class _ConnectionLeaseWaiter:
    """One task waiting for the integration-wide Bluetooth lease."""

    client: ConnectionLeaseClient
    priority: ConnectionLeasePriority
    future: asyncio.Future[None]
    granted: bool = False


class GoveeConnectionArbiter:
    """Share one retained GATT connection across purifier entries."""

    def __init__(self) -> None:
        self._lease_held = False
        self._owner: ConnectionLeaseClient | None = None
        self._command_waiters: deque[_ConnectionLeaseWaiter] = deque()
        self._poll_waiters: deque[_ConnectionLeaseWaiter] = deque()
        self._consecutive_priority_commands = 0
        self._lease_timeout: Callable[[], float] = lambda: CONNECTION_LEASE_TIMEOUT

    async def async_run(
        self,
        client: ConnectionLeaseClient,
        operation: Callable[[], Awaitable[Any]],
        deadline: float | None = None,
        *,
        priority: ConnectionLeasePriority = ConnectionLeasePriority.COMMAND,
    ) -> Any:
        """Run one client's work after releasing a different idle owner."""

        await self.async_acquire(client, deadline, priority=priority)
        try:
            return await operation()
        finally:
            self.release()

    async def async_acquire(
        self,
        client: ConnectionLeaseClient,
        deadline: float | None = None,
        *,
        priority: ConnectionLeasePriority = ConnectionLeasePriority.COMMAND,
    ) -> None:
        """Acquire the shared lease before a client transaction lock."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        queue_deadline = deadline or (started + self._lease_timeout())
        waiter = _ConnectionLeaseWaiter(client, priority, loop.create_future())
        _LOGGER.debug(
            "%s waiting for %s shared BLE connection lease",
            client._log_label,
            priority.value,
        )
        if not self._lease_held:
            self._lease_held = True
            waiter.granted = True
        else:
            self._queue_for(priority).append(waiter)
        try:
            if not waiter.granted:
                await _async_wait_until(asyncio.shield(waiter.future), queue_deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._cancel_waiter(waiter)
            _LOGGER.debug(
                "%s timed out waiting for %s shared BLE connection lease after "
                "%.2f seconds",
                client._log_label,
                priority.value,
                loop.time() - started,
            )
            raise GoveeBleClientError(
                "Timed out waiting for another purifier's Bluetooth connection"
            ) from err
        except BaseException:
            self._cancel_waiter(waiter)
            raise
        try:
            owner = self._owner
            if owner is not None and owner is not client:
                await owner._async_release_for_connection_switch(
                    loop.time() + DISCONNECT_TIMEOUT
                )
            self._owner = client
            _LOGGER.debug(
                "%s acquired %s shared BLE connection lease after %.2f seconds",
                client._log_label,
                priority.value,
                loop.time() - started,
            )
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """Release a lease acquired by :meth:`async_acquire`."""

        if not self._lease_held:
            raise RuntimeError("Shared BLE connection lease is not held")
        waiter = self._next_waiter()
        if waiter is None:
            self._lease_held = False
            return
        waiter.granted = True
        waiter.future.set_result(None)

    def _queue_for(
        self, priority: ConnectionLeasePriority
    ) -> deque[_ConnectionLeaseWaiter]:
        """Return the FIFO queue for one class of Bluetooth work."""

        if priority is ConnectionLeasePriority.COMMAND:
            return self._command_waiters
        return self._poll_waiters

    def _cancel_waiter(self, waiter: _ConnectionLeaseWaiter) -> None:
        """Remove or relinquish a waiter after timeout or cancellation."""

        if waiter.granted:
            self.release()
            return
        queue = self._queue_for(waiter.priority)
        try:
            queue.remove(waiter)
        except ValueError:
            return
        waiter.future.cancel()

    def _next_waiter(self) -> _ConnectionLeaseWaiter | None:
        """Select the next waiter, prioritizing commands without starving polls."""

        if self._command_waiters and self._poll_waiters:
            if self._consecutive_priority_commands >= MAX_PRIORITY_COMMAND_BURST:
                self._consecutive_priority_commands = 0
                return self._poll_waiters.popleft()
            self._consecutive_priority_commands += 1
            return self._command_waiters.popleft()
        if self._command_waiters:
            self._consecutive_priority_commands = 0
            return self._command_waiters.popleft()
        if self._poll_waiters:
            self._consecutive_priority_commands = 0
            return self._poll_waiters.popleft()
        self._consecutive_priority_commands = 0
        return None

    def connection_released(self, client: ConnectionLeaseClient) -> None:
        """Forget an owner after its retained connection is gone."""

        if self._owner is client:
            self._owner = None
