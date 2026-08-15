"""Focused tests for BLE connection/session ownership."""

import asyncio
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleClientError,
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth._notifications import (
    TransactionNotificationRoute,
)
from custom_components.govee_ble_air_purifier.bluetooth._session import GoveeBleSession
from custom_components.govee_ble_air_purifier.profiles import EncryptionMode


class FakeClient:
    """Minimal connected transport identity."""

    is_connected = True


def _cancel(future: asyncio.Future[Any]) -> None:
    future.cancel()


def test_session_owns_identity_generation_and_disconnect_signal() -> None:
    """A detach invalidates callbacks and wakes waiters for one exact client."""

    session = GoveeBleSession(_cancel)
    client = FakeClient()
    session.reset_for_connection_attempt()
    session.attach(client, 10.0)
    generation = session.generation
    disconnect_signal = session.disconnect_signal_for(client)

    assert disconnect_signal is not None
    assert session.is_current(client, generation)

    detached = session.detach(
        15.0,
        expected_client=client,
        signal_disconnect=True,
        unexpected=True,
    )

    assert detached is not None
    assert detached.client is client
    assert detached.connection_age == 5.0
    assert disconnect_signal.is_set()
    assert not session.is_current(client, generation)
    assert session.unexpected_disconnect_revision == 1


def test_session_ignores_stale_disconnect_identity() -> None:
    """A callback from an old client cannot detach the active transport."""

    session = GoveeBleSession(_cancel)
    active = FakeClient()
    session.attach(active, 10.0)

    assert session.detach(11.0, expected_client=FakeClient()) is None
    assert session.client is active


def test_session_binds_encryption_and_notification_ownership() -> None:
    """Connection-scoped crypto and notifications share identity validation."""

    session = GoveeBleSession(_cancel)
    client = FakeClient()
    session.attach(client, 10.0)
    generation = session.generation
    key = bytes(range(16))
    plaintext = bytes(range(20))

    session.activate_encryption(client, key, 12.0)
    encoded = session.encode(plaintext, EncryptionMode.GOVEE_V1)
    assert encoded != plaintext
    assert session.decode(encoded, EncryptionMode.GOVEE_V1) == plaintext

    session.mark_notifications_active(client, generation)
    assert session.notifications_active_for(client)
    assert session.release_notifications(client)
    assert not session.notifications_active_for(client)


def test_session_owns_exact_transaction_route() -> None:
    """Only the exact active route may be cleared by transaction cleanup."""

    session = GoveeBleSession(_cancel)
    route = TransactionNotificationRoute(
        handle_frame=lambda _frame: False,
        handle_error=lambda _err: None,
        handle_stale_handshake=lambda _command: None,
        handle_nonmatching=lambda: None,
    )
    stale_route = TransactionNotificationRoute(
        handle_frame=lambda _frame: False,
        handle_error=lambda _err: None,
        handle_stale_handshake=lambda _command: None,
        handle_nonmatching=lambda: None,
    )

    session.bind_transaction_route(route)
    session.unbind_transaction_route(stale_route)
    assert session.transaction_route is route
    with pytest.raises(GoveeBleClientError, match="route is already active"):
        session.bind_transaction_route(stale_route)

    session.unbind_transaction_route(route)
    assert session.transaction_route is None


def test_session_rejects_crypto_without_negotiated_key() -> None:
    """Encrypted application traffic cannot escape the handshake lifecycle."""

    session = GoveeBleSession(_cancel)

    with pytest.raises(GoveeBleClientError, match="session is unavailable"):
        session.encode(bytes(20), EncryptionMode.GOVEE_V1)


def test_malformed_listener_invalidates_only_its_generation() -> None:
    """Recovery rejects queued callbacks without dropping transport identity yet."""

    session = GoveeBleSession(_cancel)
    client = FakeClient()
    session.attach(client, 10.0)
    generation = session.generation
    session.mark_notifications_active(client, generation)

    assert session.invalidate_notifications(client, generation)
    assert session.client is client
    assert not session.is_current(client, generation)
    assert not session.notifications_active_for(client)
    assert not session.invalidate_notifications(client, generation)


@pytest.mark.asyncio
async def test_session_wait_disconnect_wins_and_cancels_operation() -> None:
    """The exact session disconnect signal preempts a pending backend operation."""

    session = GoveeBleSession(_cancel)
    signal = asyncio.Event()

    async def pending() -> None:
        await asyncio.Event().wait()

    task = asyncio.create_task(
        session.async_wait(pending(), signal, asyncio.get_running_loop().time() + 1.0)
    )
    await asyncio.sleep(0)
    signal.set()

    with pytest.raises(GoveeBleDisconnectedError):
        await task
    assert session.quarantined_operation_count == 0


@pytest.mark.asyncio
async def test_session_wait_deadline_quarantines_cancellation_resistant_operation() -> (
    None
):
    """A stuck backend call is retained until it acknowledges cancellation."""

    release = asyncio.Event()

    async def cancel_and_wait() -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    session = GoveeBleSession(_cancel)
    with pytest.raises(TimeoutError):
        await session.async_wait(
            cancel_and_wait(), None, asyncio.get_running_loop().time() + 0.01
        )
    assert session.quarantined_operation_count == 1

    release.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    session.reap_abandoned_operations()
    assert session.quarantined_operation_count == 0
