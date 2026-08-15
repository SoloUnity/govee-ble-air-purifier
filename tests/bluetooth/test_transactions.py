"""Focused tests for the connection-independent transaction runner."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from typing import Any, TypeVar

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleClientError,
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth._notifications import (
    TransactionNotificationRoute,
)
from custom_components.govee_ble_air_purifier.bluetooth._transactions import (
    ExchangePlan,
    ExchangeRequest,
    TransactionRunner,
)
from custom_components.govee_ble_air_purifier.govee_ble_air_purifier_protocol import (
    NightLightPollingRequestOrder,
)

_T = TypeVar("_T")


class FakeExchangeSession:
    """Drive transaction notifications without exposing a BLE client."""

    def __init__(
        self,
        responses: dict[bytes, bytes],
        *,
        persistent: bool = False,
        fail_write: bytes | None = None,
        fail_stop: bool = False,
    ) -> None:
        self.responses = responses
        self.persistent_notifications_enabled = persistent
        self.fail_write = fail_write
        self.fail_stop = fail_stop
        self.is_connected = True
        self.disconnect_signal: asyncio.Event | None = asyncio.Event()
        self.route: TransactionNotificationRoute | None = None
        self.writes: list[bytes] = []
        self.started = 0
        self.stopped = 0

    async def async_start_notifications(self, _deadline: float) -> None:
        self.started += 1

    async def async_stop_notifications(self, _deadline: float) -> None:
        self.stopped += 1
        if self.fail_stop:
            raise RuntimeError("stop failed")

    async def async_write(self, command: bytes, _deadline: float) -> None:
        self.writes.append(command)
        if command == self.fail_write:
            self.is_connected = False
            raise GoveeBleDisconnectedError("disconnected")
        response = self.responses.get(command)
        if response is not None:
            assert self.route is not None
            self.route.handle_frame(response)

    async def async_wait(self, awaitable: Awaitable[_T], deadline: float) -> _T:
        remaining = deadline - asyncio.get_running_loop().time()
        return await asyncio.wait_for(awaitable, max(0.0, remaining))

    def bind_route(self, route: TransactionNotificationRoute) -> None:
        assert self.route is None
        self.route = route

    def unbind_route(self, route: TransactionNotificationRoute) -> None:
        if self.route is route:
            self.route = None


def _runner(logs: list[tuple[Any, ...]]) -> TransactionRunner:
    def debug(*args: Any, **_kwargs: Any) -> None:
        logs.append(args)

    def stage(*args: Any) -> None:
        logs.append(args)

    return TransactionRunner(
        log_label="H7124 [device]",
        debug=debug,
        log_stage=stage,
        log_timeout=stage,
        log_failure=stage,
        timeout_message=lambda stage_name: f"timeout: {stage_name}",
    )


@pytest.mark.asyncio
async def test_runner_collects_required_and_pipelined_optional_frames() -> None:
    required_command = b"required"
    optional_one = b"optional-one"
    optional_two = b"optional-two"
    responses = {
        required_command: b"required-response",
        optional_one: b"optional-one-response",
        optional_two: b"optional-two-response",
    }
    session = FakeExchangeSession(responses)

    result = await _runner([]).async_exchange(
        session,
        ExchangePlan(
            required=(
                ExchangeRequest(
                    required_command, lambda frame: frame == responses[required_command]
                ),
            ),
            optional=(
                ExchangeRequest(
                    optional_one, lambda frame: frame == responses[optional_one]
                ),
                ExchangeRequest(
                    optional_two, lambda frame: frame == responses[optional_two]
                ),
            ),
            optional_timeout=0.1,
            optional_order=NightLightPollingRequestOrder.PIPELINED,
        ),
    )

    assert result.frames == tuple(responses.values())
    assert result.discard_session is False
    assert session.writes == [required_command, optional_one, optional_two]
    assert session.started == session.stopped == 1
    assert session.route is None


@pytest.mark.asyncio
async def test_runner_keeps_persistent_listener_and_counts_late_handshake() -> None:
    command = b"command"
    response = b"response"
    logs: list[tuple[Any, ...]] = []
    session = FakeExchangeSession({}, persistent=True)
    original_write = session.async_write

    async def write_with_stale_handshake(frame: bytes, deadline: float) -> None:
        await original_write(frame, deadline)
        assert session.route is not None
        session.route.handle_stale_handshake(0x02)
        session.route.handle_frame(response)

    session.async_write = write_with_stale_handshake  # type: ignore[method-assign]

    result = await _runner(logs).async_exchange(
        session,
        ExchangePlan(
            required=(ExchangeRequest(command, lambda frame: frame == response),),
        ),
    )

    assert result.frames == (response,)
    assert session.stopped == 0
    assert any("stale handshakes: %d" in entry[0] and 1 in entry for entry in logs)


@pytest.mark.asyncio
async def test_notification_cleanup_failure_preserves_frames_and_discards_session() -> (
    None
):
    command = b"command"
    response = b"response"
    session = FakeExchangeSession({command: response}, fail_stop=True)

    result = await _runner([]).async_exchange(
        session,
        ExchangePlan(
            required=(ExchangeRequest(command, lambda frame: frame == response),),
        ),
    )

    assert result.frames == (response,)
    assert result.discard_session is True
    assert session.stopped == 1


@pytest.mark.asyncio
async def test_optional_disconnect_preserves_required_frame_without_replay() -> None:
    required_command = b"required"
    optional_command = b"optional"
    response = b"required-response"
    session = FakeExchangeSession(
        {required_command: response}, fail_write=optional_command
    )

    result = await _runner([]).async_exchange(
        session,
        ExchangePlan(
            required=(
                ExchangeRequest(required_command, lambda frame: frame == response),
            ),
            optional=(ExchangeRequest(optional_command, lambda _frame: True),),
            optional_timeout=0.1,
        ),
    )

    assert result.frames == (response, None)
    assert result.discard_session is True
    assert session.writes == [required_command, optional_command]


@pytest.mark.asyncio
async def test_required_timeout_reports_exact_transaction_stage() -> None:
    session = FakeExchangeSession({})

    with pytest.raises(GoveeBleClientError, match=r"timeout: waiting for response 1/1"):
        await _runner([]).async_exchange(
            session,
            ExchangePlan(
                required=(ExchangeRequest(b"command", lambda _frame: True),),
                timeout=0.001,
            ),
        )

    assert session.writes == [b"command"]
    assert session.route is None
