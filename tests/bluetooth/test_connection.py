"""Focused tests for connection lifecycle and recovery orchestration."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import time
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleClientError,
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth._connection import (
    ConnectionDependencies,
    ConnectionManager,
)
from custom_components.govee_ble_air_purifier.bluetooth._session import GoveeBleSession
from custom_components.govee_ble_air_purifier.profiles import EncryptionMode


class FakeClient:
    """Minimal transport controlled by the lifecycle harness."""

    def __init__(self, *, connected: bool = True, stop_error: bool = False) -> None:
        self.is_connected = connected
        self.stop_error = stop_error
        self.stopped: list[str] = []

    async def stop_notify(self, uuid: str) -> None:
        self.stopped.append(uuid)
        if self.stop_error:
            raise RuntimeError("stop failed")


@dataclass
class Harness:
    """Fake integration boundary for direct manager tests."""

    client: FakeClient = field(default_factory=FakeClient)
    encryption: EncryptionMode = EncryptionMode.NONE
    closed: bool = False
    negotiation_error: BaseException | None = None
    establish_calls: int = 0
    negotiation_calls: int = 0
    disconnects: list[tuple[FakeClient, float]] = field(default_factory=list)
    released: int = 0
    stale: int = 0
    idle_scheduled: int = 0
    leases_acquired: int = 0
    leases_released: int = 0
    stages: list[str] = field(default_factory=list)

    async def establish(self, callback: Any, deadline: float) -> FakeClient:
        self.establish_calls += 1
        self.disconnect_callback = callback
        return self.client

    async def negotiate(self, client: Any, deadline: float) -> bytes:
        self.negotiation_calls += 1
        if self.negotiation_error is not None:
            raise self.negotiation_error
        return bytes(range(16))

    async def wait(
        self,
        awaitable: Any,
        signal: asyncio.Event | None,
        deadline: float,
    ) -> Any:
        return await awaitable

    async def disconnect(self, client: Any, deadline: float) -> None:
        self.disconnects.append((client, deadline))
        client.is_connected = False

    async def acquire_lease(self) -> None:
        self.leases_acquired += 1

    def release_lease(self) -> None:
        self.leases_released += 1

    def create_task(self, coroutine: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine)

    def dependencies(self) -> ConnectionDependencies:
        return ConnectionDependencies(
            establish=self.establish,
            negotiate_session=self.negotiate,
            wait_for_connection=self.wait,
            disconnect=self.disconnect,
            acquire_recovery_lease=self.acquire_lease,
            release_recovery_lease=self.release_lease,
            connection_released=lambda: setattr(self, "released", self.released + 1),
            mark_connection_stale=lambda: setattr(self, "stale", self.stale + 1),
            schedule_idle_disconnect=lambda: setattr(
                self, "idle_scheduled", self.idle_scheduled + 1
            ),
            create_task=self.create_task,
            is_closed=lambda: self.closed,
            encryption_mode=lambda: self.encryption,
            notify_char_uuid=lambda: "notify",
            handshake_timeout=lambda: 1.0,
            disconnect_timeout=lambda: 1.0,
            monotonic=time.monotonic,
            log_stage=lambda _operation, stage, _started, _deadline: (
                self.stages.append(stage)
            ),
        )


def manager(
    harness: Harness,
) -> tuple[ConnectionManager, GoveeBleSession, asyncio.Lock]:
    session = GoveeBleSession(lambda future: future.cancel())
    lock = asyncio.Lock()
    return (
        ConnectionManager(
            session,
            lock,
            harness.dependencies(),
            log_label="H7124 [test]",
            logger=logging.getLogger(__name__),
        ),
        session,
        lock,
    )


@pytest.mark.asyncio
async def test_manager_connects_once_then_reuses_without_replaying_operation() -> None:
    """Connection establishment is reusable while each operation runs once."""

    harness = Harness()
    lifecycle, session, _lock = manager(harness)
    attempts = 0

    async def operation(client: Any) -> str:
        nonlocal attempts
        attempts += 1
        assert client is harness.client
        return "ok"

    assert await lifecycle.async_run(operation, deadline=10.0) == "ok"
    assert await lifecycle.async_run(operation, deadline=10.0) == "ok"
    assert harness.establish_calls == 1
    assert attempts == 2
    assert harness.idle_scheduled == 2
    assert session.client is harness.client


@pytest.mark.asyncio
async def test_manager_negotiates_encryption_before_operation() -> None:
    """An encrypted profile binds its key before exposing the client."""

    harness = Harness(encryption=EncryptionMode.GOVEE_V1)
    lifecycle, session, _lock = manager(harness)

    async def operation(_client: Any) -> bytes | None:
        return session.session_key

    assert await lifecycle.async_run(operation, deadline=10.0) == bytes(range(16))
    assert harness.negotiation_calls == 1
    assert harness.stages == [
        "establishing transport",
        "transport connected",
        "encrypted session ready",
    ]


@pytest.mark.asyncio
async def test_manager_rejects_transport_that_disconnects_while_connecting() -> None:
    """A dead-on-arrival transport is detached before an operation can run."""

    harness = Harness(client=FakeClient(connected=False))
    lifecycle, session, _lock = manager(harness)

    async def operation(_client: Any) -> None:
        raise AssertionError("operation must not run")

    with pytest.raises(GoveeBleDisconnectedError, match="while connecting"):
        await lifecycle.async_run(operation, deadline=10.0)
    assert session.client is None
    assert len(harness.disconnects) == 1
    assert harness.disconnects[0][0] is harness.client


@pytest.mark.asyncio
async def test_manager_maps_handshake_timeout_and_drops_connection() -> None:
    """Handshake timeout keeps its established-session error contract."""

    harness = Harness(
        encryption=EncryptionMode.GOVEE_V1,
        negotiation_error=TimeoutError(),
    )
    lifecycle, session, _lock = manager(harness)

    async def operation(_client: Any) -> None:
        raise AssertionError("operation must not run")

    with pytest.raises(GoveeBleClientError, match="Timed out establishing"):
        await lifecycle.async_run(operation, deadline=10.0)
    assert session.client is None
    assert len(harness.disconnects) == 1


@pytest.mark.asyncio
async def test_operation_failure_drops_once_without_retry() -> None:
    """Connection recovery invalidates transport but never replays commands."""

    harness = Harness()
    lifecycle, _session, _lock = manager(harness)
    attempts = 0

    async def operation(_client: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("failed")

    with pytest.raises(ValueError, match="failed"):
        await lifecycle.async_run(operation, deadline=10.0)
    assert attempts == 1
    assert harness.establish_calls == 1
    assert len(harness.disconnects) == 1


@pytest.mark.asyncio
async def test_drop_suppresses_notification_cleanup_failure() -> None:
    """Listener cleanup cannot prevent the separately bounded disconnect."""

    harness = Harness(client=FakeClient(stop_error=True))
    lifecycle, session, _lock = manager(harness)
    session.attach(harness.client, asyncio.get_running_loop().time())
    session.mark_notifications_active(harness.client, session.generation)

    await lifecycle.async_drop(deadline=10.0)

    assert harness.client.stopped == ["notify"]
    assert len(harness.disconnects) == 1
    assert harness.released == 1
    assert harness.stale == 1


def test_unexpected_disconnect_is_exact_client_scoped() -> None:
    """Stale callbacks cannot invalidate a newer connection generation."""

    harness = Harness()
    lifecycle, session, _lock = manager(harness)
    session.attach(harness.client, 10.0)
    signal = session.disconnect_signal

    lifecycle.handle_disconnect(FakeClient())
    assert session.client is harness.client

    lifecycle.handle_disconnect(harness.client)
    assert session.client is None
    assert signal is not None and signal.is_set()
    assert session.unexpected_disconnect_revision == 1
    assert harness.released == 1
    assert harness.stale == 1


@pytest.mark.asyncio
async def test_malformed_listener_recovery_uses_lease_lock_and_bounded_drop() -> None:
    """Idle-listener corruption is serialized before exact-client cleanup."""

    harness = Harness()
    lifecycle, session, lock = manager(harness)
    session.attach(harness.client, asyncio.get_running_loop().time())
    generation = session.generation
    session.mark_notifications_active(harness.client, generation)

    lifecycle.schedule_notification_recovery(harness.client, generation)
    task = lifecycle.notification_recovery_task
    assert task is not None
    await task
    await asyncio.sleep(0)

    assert harness.client.stopped == ["notify"]
    assert len(harness.disconnects) == 1
    assert harness.leases_acquired == harness.leases_released == 1
    assert not lock.locked()
    assert not lifecycle.notification_recovery_active
