"""Connection establishment, reuse, invalidation, and recovery orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass
import logging
from typing import Any, TypeVar

from ..govee_ble_air_purifier_protocol import EncryptionMode
from . import GoveeBleClientError, GoveeBleDisconnectedError
from ._session import DetachedConnection, GoveeBleSession
from .transport import _async_wait_until

_T = TypeVar("_T")
DisconnectCallback = Callable[[Any], None]
WaitForConnection = Callable[
    [Awaitable[Any], asyncio.Event | None, float], Awaitable[Any]
]
TaskFactory = Callable[[Coroutine[Any, Any, Any]], asyncio.Task[Any]]
StageLogger = Callable[[str, str, float, float], None]


@dataclass(frozen=True, slots=True)
class ConnectionDependencies:
    """Integration-specific operations required by the lifecycle manager."""

    establish: Callable[[DisconnectCallback, float], Awaitable[Any]]
    negotiate_session: Callable[[Any, float], Awaitable[bytes]]
    wait_for_connection: WaitForConnection
    disconnect: Callable[[Any, float], Awaitable[None]]
    acquire_recovery_lease: Callable[[], Awaitable[None]]
    release_recovery_lease: Callable[[], None]
    connection_released: Callable[[], None]
    mark_connection_stale: Callable[[], None]
    schedule_idle_disconnect: Callable[[], None]
    create_task: TaskFactory
    is_closed: Callable[[], bool]
    encryption_mode: Callable[[], EncryptionMode]
    notify_char_uuid: Callable[[], str]
    handshake_timeout: Callable[[], float]
    disconnect_timeout: Callable[[], float]
    monotonic: Callable[[], float]
    log_stage: StageLogger


class ConnectionManager:
    """Own connection lifecycle policy without knowing transaction semantics."""

    def __init__(
        self,
        session: GoveeBleSession,
        lock: asyncio.Lock,
        dependencies: ConnectionDependencies,
        *,
        log_label: str,
        logger: logging.Logger,
    ) -> None:
        self._session = session
        self._lock = lock
        self._dependencies = dependencies
        self._log_label = log_label
        self._logger = logger
        self._notification_recovery_task: asyncio.Task[Any] | None = None
        self._notification_recovery_started_at: float | None = None

    @property
    def notification_recovery_task(self) -> asyncio.Task[Any] | None:
        """Return the current malformed-listener recovery task, if any."""

        return self._notification_recovery_task

    @property
    def notification_recovery_started_at(self) -> float | None:
        """Return when the current recovery started."""

        return self._notification_recovery_started_at

    @property
    def notification_recovery_active(self) -> bool:
        """Return whether malformed-listener recovery still owns cleanup."""

        task = self._notification_recovery_task
        return task is not None and not task.done()

    def notification_recovery_age(self, now: float) -> float | None:
        """Return the active recovery age for non-sensitive diagnostics."""

        started_at = self._notification_recovery_started_at
        if not self.notification_recovery_active or started_at is None:
            return None
        return max(0.0, now - started_at)

    async def async_run(
        self, operation: Callable[[Any], Awaitable[_T]], *, deadline: float
    ) -> _T:
        """Run one operation once against a reusable connected session."""

        self._require_available()
        client = await self._async_get_or_connect(deadline)
        if self._dependencies.is_closed():
            await self.async_drop(
                self._now() + self._disconnect_timeout(), prepare_reconnect=False
            )
            raise GoveeBleClientError("BLE client is closed")
        result = await self._async_execute_once(client, operation)
        if self._dependencies.is_closed():
            await self.async_drop(
                self._now() + self._disconnect_timeout(), prepare_reconnect=False
            )
        else:
            self._dependencies.schedule_idle_disconnect()
        return result

    def _require_available(self) -> None:
        """Reject new work while closed or listener cleanup is active."""

        if self._dependencies.is_closed():
            raise GoveeBleClientError("BLE client is closed")
        self._session.reap_abandoned_operations()
        if self.notification_recovery_active:
            raise GoveeBleClientError(
                "Previous BLE notification listener is still recovering"
            )

    async def _async_get_or_connect(self, deadline: float) -> Any:
        """Return the healthy cached client or establish a replacement."""

        client = self._session.client
        if client is not None and client.is_connected:
            self._logger.debug(
                "%s reusing active BLE connection", self._log_label
            )
            return client
        return await self._async_connect(deadline)

    async def _async_connect(self, deadline: float) -> Any:
        """Establish transport and its optional encrypted session."""

        started = self._now()
        revision = self._session.unexpected_disconnect_revision
        self._dependencies.log_stage(
            "BLE connection", "establishing transport", started, deadline
        )
        self._session.reset_for_connection_attempt()
        client = await self._dependencies.wait_for_connection(
            self._dependencies.establish(self.handle_disconnect, deadline),
            None,
            deadline,
        )
        self._session.attach(client, self._now())
        self._dependencies.log_stage(
            "BLE connection", "transport connected", started, deadline
        )
        if not client.is_connected:
            await self.async_drop(self._now() + self._disconnect_timeout())
            raise GoveeBleDisconnectedError("Purifier disconnected while connecting")
        if self._dependencies.encryption_mode() is EncryptionMode.GOVEE_V1:
            await self._async_start_encrypted_session(client, revision, started)
        return client

    async def _async_start_encrypted_session(
        self, client: Any, connection_revision: int, connection_started: float
    ) -> None:
        """Negotiate and bind one connection-scoped encryption key."""

        deadline = self._now() + self._dependencies.handshake_timeout()
        try:
            session_key = await self._dependencies.negotiate_session(client, deadline)
            self._session.activate_encryption(client, session_key, self._now())
            self._dependencies.log_stage(
                "BLE connection",
                "encrypted session ready",
                connection_started,
                deadline,
            )
        except (TimeoutError, asyncio.TimeoutError) as err:
            disconnected = await self.async_drop_after_error(
                client, connection_revision
            )
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during encrypted-session setup"
                ) from err
            raise GoveeBleClientError(
                "Timed out establishing encrypted purifier session"
            ) from err
        except GoveeBleClientError as err:
            disconnected = await self.async_drop_after_error(
                client, connection_revision
            )
            if disconnected and not isinstance(err, GoveeBleDisconnectedError):
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during encrypted-session setup"
                ) from err
            raise
        except Exception as err:
            disconnected = await self.async_drop_after_error(
                client, connection_revision
            )
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during encrypted-session setup"
                ) from err
            raise GoveeBleClientError(
                "Failed to establish encrypted purifier session"
            ) from err
        except BaseException:
            await self.async_drop(self._now() + self._disconnect_timeout())
            raise

    async def _async_execute_once(
        self, client: Any, operation: Callable[[Any], Awaitable[_T]]
    ) -> _T:
        """Execute exactly once and normalize disconnect/timeout failures."""

        revision = self._session.unexpected_disconnect_revision
        try:
            return await operation(client)
        except (TimeoutError, asyncio.TimeoutError) as err:
            disconnected = await self.async_drop_after_error(client, revision)
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                ) from err
            raise GoveeBleClientError("Timed out during BLE transaction") from err
        except Exception as err:
            disconnected = await self.async_drop_after_error(client, revision)
            if disconnected:
                raise GoveeBleDisconnectedError(
                    "Purifier disconnected during BLE transaction"
                ) from err
            raise
        except BaseException:
            await self.async_drop(self._now() + self._disconnect_timeout())
            raise

    async def async_drop_after_error(
        self, client: Any, disconnect_revision: int
    ) -> bool:
        """Drop a failed connection after allowing its callback to run."""

        try:
            disconnected = self._was_disconnected(client, disconnect_revision)
            if not disconnected:
                await asyncio.sleep(0)
                disconnected = self._was_disconnected(client, disconnect_revision)
            return disconnected
        finally:
            await self.async_drop(self._now() + self._disconnect_timeout())

    def _was_disconnected(self, client: Any, revision: int) -> bool:
        return (
            self._session.unexpected_disconnect_revision != revision
            or not client.is_connected
        )

    async def async_drop(
        self, deadline: float, *, prepare_reconnect: bool = True
    ) -> None:
        """Detach first, then best-effort stop notifications and disconnect."""

        detached = self._session.detach(self._dependencies.monotonic())
        assert detached is not None
        self._dependencies.connection_released()
        if detached.client is None:
            return
        self._log_detached_connection(detached)
        await self._async_cleanup_notifications(detached, deadline)
        disconnect_deadline = self._now() + self._disconnect_timeout()
        await self._dependencies.disconnect(detached.client, disconnect_deadline)
        if prepare_reconnect:
            self._dependencies.mark_connection_stale()

    async def _async_cleanup_notifications(
        self, detached: DetachedConnection, deadline: float
    ) -> None:
        """Best-effort stop a listener within the caller's cleanup budget."""

        client = detached.client
        if client is None or not detached.notifications_active or not client.is_connected:
            return
        try:
            await self._dependencies.wait_for_connection(
                client.stop_notify(self._dependencies.notify_char_uuid()),
                None,
                deadline,
            )
        except Exception:
            self._logger.debug(
                "%s suppressing BLE notification cleanup failure",
                self._log_label,
                exc_info=True,
            )

    def _log_detached_connection(self, detached: DetachedConnection) -> None:
        connection_age = detached.connection_age
        session_age = detached.encrypted_session_age
        self._logger.debug(
            "%s releasing cached BLE connection%s%s",
            self._log_label,
            (
                f" after {connection_age:.2f} seconds"
                if connection_age is not None
                else ""
            ),
            (
                f" (encrypted session age: {session_age:.2f} seconds)"
                if session_age is not None
                else ""
            ),
        )

    def handle_disconnect(self, client: Any) -> None:
        """Forget only the exact transport named by a disconnect callback."""

        detached = self._session.detach(
            self._dependencies.monotonic(),
            expected_client=client,
            signal_disconnect=True,
            unexpected=True,
        )
        if detached is None:
            return
        connection_age = detached.connection_age or 0.0
        session_age = detached.encrypted_session_age
        self._logger.debug(
            "%s BLE connection disconnected after %.2f seconds%s",
            self._log_label,
            connection_age,
            (
                f" (encrypted session age: {session_age:.2f} seconds)"
                if session_age is not None
                else ""
            ),
        )
        self._dependencies.connection_released()
        self._dependencies.mark_connection_stale()

    def schedule_notification_recovery(self, client: Any, generation: int) -> None:
        """Invalidate a malformed idle listener and schedule bounded cleanup."""

        if self._dependencies.is_closed() or not self._session.invalidate_notifications(
            client, generation
        ):
            return
        if self.notification_recovery_active:
            return
        self._logger.debug(
            "%s invalidating application notifications after malformed frame",
            self._log_label,
        )
        task = self._dependencies.create_task(
            self._async_recover_notifications(client)
        )
        self._notification_recovery_task = task
        self._notification_recovery_started_at = self._dependencies.monotonic()
        task.add_done_callback(self._clear_notification_recovery)

    async def _async_recover_notifications(self, client: Any) -> None:
        await self._dependencies.acquire_recovery_lease()
        try:
            deadline = self._now() + self._disconnect_timeout()
            await self._async_acquire_recovery_lock(deadline)
            try:
                if self._session.is_current(client):
                    await self._async_stop_malformed_listener(client, deadline)
                    await self.async_drop(deadline)
            finally:
                self._lock.release()
        finally:
            self._dependencies.release_recovery_lease()

    async def _async_acquire_recovery_lock(self, deadline: float) -> None:
        try:
            await _async_wait_until(self._lock.acquire(), deadline)
        except (TimeoutError, asyncio.TimeoutError) as err:
            raise GoveeBleClientError(
                "Timed out waiting for notification recovery lock"
            ) from err

    async def _async_stop_malformed_listener(
        self, client: Any, deadline: float
    ) -> None:
        if not client.is_connected:
            return
        try:
            await self._dependencies.wait_for_connection(
                client.stop_notify(self._dependencies.notify_char_uuid()),
                None,
                deadline,
            )
        except Exception:
            self._logger.debug(
                "%s suppressing malformed-listener cleanup failure",
                self._log_label,
                exc_info=True,
            )

    def _clear_notification_recovery(self, completed: asyncio.Task[Any]) -> None:
        if self._notification_recovery_task is completed:
            self._notification_recovery_task = None
            self._notification_recovery_started_at = None
        if completed.cancelled():
            return
        error = completed.exception()
        if error is not None:
            self._logger.debug(
                "%s notification recovery failed",
                self._log_label,
                exc_info=(type(error), error, error.__traceback__),
            )

    async def async_cancel_notification_recovery(self) -> None:
        """Cancel and await the manager-owned recovery task during shutdown."""

        task = self._notification_recovery_task
        self._notification_recovery_task = None
        self._notification_recovery_started_at = None
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def compat_set_notification_recovery_task(
        self, task: asyncio.Task[Any] | None
    ) -> None:
        """Preserve the former private field as a test seam."""

        self._notification_recovery_task = task

    def compat_set_notification_recovery_started_at(
        self, started_at: float | None
    ) -> None:
        """Preserve the former private timestamp as a test seam."""

        self._notification_recovery_started_at = started_at

    def _disconnect_timeout(self) -> float:
        return self._dependencies.disconnect_timeout()

    @staticmethod
    def _now() -> float:
        return asyncio.get_running_loop().time()
