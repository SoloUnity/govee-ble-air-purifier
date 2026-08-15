"""Typed request/response transaction execution over one BLE session."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, TypeVar, cast

from ..govee_ble_air_purifier_protocol import (
    EncryptionMode,
    NightLightPollingRequestOrder,
    ProtocolError,
)
from . import GoveeBleClientError, GoveeBleDisconnectedError
from ._notifications import TransactionNotificationRoute
from ._session import GoveeBleSession

_T = TypeVar("_T")
FrameMatcher = Callable[[bytes], bool]
StageLogger = Callable[[str, str, float, float], None]
FailureLogger = Callable[[str, str, float], None]
DebugLogger = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class ExchangeRequest:
    """One plaintext request and the notification predicate that confirms it."""

    command: bytes
    matcher: FrameMatcher


@dataclass(frozen=True, slots=True)
class ExchangePlan:
    """Required and best-effort requests sharing one application transaction."""

    required: tuple[ExchangeRequest, ...]
    optional: tuple[ExchangeRequest, ...] = ()
    timeout: float = 10.0
    optional_timeout: float = 0.0
    optional_order: NightLightPollingRequestOrder = (
        NightLightPollingRequestOrder.PIPELINED
    )
    cleanup_timeout: float = 2.0

    @property
    def request_count(self) -> int:
        """Return the total number of required and optional requests."""

        return len(self.required) + len(self.optional)


@dataclass(frozen=True, slots=True)
class ExchangeResult:
    """Collected frames and whether the caller must discard the session."""

    frames: tuple[bytes | None, ...]
    discard_session: bool = False


class ExchangeSession(Protocol):
    """Narrow connected-session contract consumed by the transaction runner."""

    @property
    def is_connected(self) -> bool: ...

    @property
    def disconnect_signal(self) -> asyncio.Event | None: ...

    @property
    def persistent_notifications_enabled(self) -> bool: ...

    async def async_start_notifications(self, deadline: float) -> None: ...

    async def async_stop_notifications(self, deadline: float) -> None: ...

    async def async_write(self, command: bytes, deadline: float) -> None: ...

    async def async_wait(self, awaitable: Awaitable[_T], deadline: float) -> _T: ...

    def bind_route(self, route: TransactionNotificationRoute) -> None: ...

    def unbind_route(self, route: TransactionNotificationRoute) -> None: ...


@dataclass(slots=True)
class ConnectedTransactionSession:
    """Adapt one exact :class:`GoveeBleSession` connection for exchanges."""

    session: GoveeBleSession
    client: Any
    write_char_uuid: str
    encryption: EncryptionMode
    persistent_notifications_enabled: bool
    ensure_notifications: Callable[[Any, float], Awaitable[None]]
    stop_notifications: Callable[[Any, float], Awaitable[None]]

    @property
    def is_connected(self) -> bool:
        """Return whether this exact transport still reports connected."""

        return bool(self.client.is_connected)

    @property
    def disconnect_signal(self) -> asyncio.Event | None:
        """Return the disconnect signal bound to this exact transport."""

        return self.session.disconnect_signal_for(self.client)

    async def async_start_notifications(self, deadline: float) -> None:
        """Ensure the connection-generation application listener is active."""

        await self.ensure_notifications(self.client, deadline)

    async def async_stop_notifications(self, deadline: float) -> None:
        """Stop the application listener for a non-persistent session."""

        await self.stop_notifications(self.client, deadline)

    async def async_write(self, command: bytes, deadline: float) -> None:
        """Encode and write one plaintext request within the shared deadline."""

        await self.session.async_wait(
            self.client.write_gatt_char(
                self.write_char_uuid,
                self.session.encode(command, self.encryption),
                response=False,
            ),
            self.disconnect_signal,
            deadline,
        )

    async def async_wait(self, awaitable: Awaitable[_T], deadline: float) -> _T:
        """Race a transaction wait against this connection's disconnect."""

        return cast(
            _T,
            await self.session.async_wait(awaitable, self.disconnect_signal, deadline),
        )

    def bind_route(self, route: TransactionNotificationRoute) -> None:
        """Publish one transaction-scoped notification route."""

        self.session.bind_transaction_route(route)

    def unbind_route(self, route: TransactionNotificationRoute) -> None:
        """Remove a route only if it remains the active transaction route."""

        self.session.unbind_transaction_route(route)


@dataclass(slots=True)
class _ExchangeState:
    """Mutable state shared by notification callbacks and exchange awaits."""

    plan: ExchangePlan
    frames: list[bytes | None] = field(init=False)
    required_future: asyncio.Future[bytes] | None = None
    optional_futures: list[asyncio.Future[bytes]] = field(default_factory=list)
    collecting_optional: bool = False
    initiated_optional_count: int = 0
    current_request_index: int = 0
    request_started: float | None = None
    notification_count: int = 0
    ignored_handshake_count: int = 0
    nonmatching_notification_count: int = 0
    discard_session: bool = False
    stage: str = "starting application notifications"

    def __post_init__(self) -> None:
        self.frames = [None] * self.plan.request_count

    def reset_required(self, index: int, future: asyncio.Future[bytes]) -> None:
        """Begin matching one required request and reset its diagnostics."""

        self.required_future = future
        self.current_request_index = index
        self.request_started = asyncio.get_running_loop().time()
        self.notification_count = 0
        self.ignored_handshake_count = 0
        self.nonmatching_notification_count = 0

    def begin_optional(self) -> None:
        """Switch notification matching to best-effort optional requests."""

        loop = asyncio.get_running_loop()
        self.collecting_optional = True
        self.required_future = None
        self.optional_futures = [loop.create_future() for _ in self.plan.optional]

    def handle_frame(self, frame: bytes) -> bool:
        """Match one decoded frame against the active required/optional request."""

        if self.required_future is None and not self.collecting_optional:
            return False
        self.notification_count += 1
        if self.collecting_optional:
            return self._handle_optional_frame(frame)
        request = self.plan.required[self.current_request_index - 1]
        if not request.matcher(frame):
            return False
        future = self.required_future
        if future is None or future.done():
            return False
        future.set_result(frame)
        return True

    def _handle_optional_frame(self, frame: bytes) -> bool:
        for index, (request, future) in enumerate(
            zip(
                self.plan.optional[: self.initiated_optional_count],
                self.optional_futures[: self.initiated_optional_count],
                strict=True,
            ),
            start=len(self.plan.required),
        ):
            if future.done() or not request.matcher(frame):
                continue
            self.frames[index] = frame
            future.set_result(frame)
            return True
        return False

    def handle_error(self, err: ProtocolError) -> None:
        """Fail an active required wait or quarantine optional corruption."""

        self.discard_session = True
        self.notification_count += 1
        if self.collecting_optional:
            self.nonmatching_notification_count += 1
            return
        future = self.required_future
        if future is not None and not future.done():
            future.set_exception(err)

    def handle_stale_handshake(self, _command: int) -> None:
        """Record valid late H7129 handshake traffic without failing exchange."""

        self.notification_count += 1
        self.ignored_handshake_count += 1

    def handle_nonmatching(self) -> None:
        """Record a decoded notification that matched no active request or push."""

        if self.required_future is None and not self.collecting_optional:
            return
        self.nonmatching_notification_count += 1

    def cancel_futures(self) -> None:
        """Cancel all transaction-owned futures still awaiting notifications."""

        if self.required_future is not None and not self.required_future.done():
            self.required_future.cancel()
        for future in self.optional_futures:
            if not future.done():
                future.cancel()

    def route(self) -> TransactionNotificationRoute:
        """Build the callback contract consumed by the persistent listener."""

        return TransactionNotificationRoute(
            handle_frame=self.handle_frame,
            handle_error=self.handle_error,
            handle_stale_handshake=self.handle_stale_handshake,
            handle_nonmatching=self.handle_nonmatching,
        )


class TransactionRunner:
    """Execute one exchange without owning connection or replay policy."""

    def __init__(
        self,
        *,
        log_label: str,
        debug: DebugLogger,
        log_stage: StageLogger,
        log_timeout: FailureLogger,
        log_failure: FailureLogger,
        timeout_message: Callable[[str], str],
    ) -> None:
        self._log_label = log_label
        self._debug = debug
        self._log_stage = log_stage
        self._log_timeout = log_timeout
        self._log_failure = log_failure
        self._timeout_message = timeout_message

    async def async_exchange(
        self, session: ExchangeSession, plan: ExchangePlan
    ) -> ExchangeResult:
        """Execute required requests, then collect best-effort optional telemetry."""

        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + plan.timeout
        state = _ExchangeState(plan)
        route = state.route()
        primary_error: BaseException | None = None
        cleanup_deadline = deadline
        try:
            self._log_stage("BLE transaction", state.stage, started, deadline)
            await session.async_start_notifications(deadline)
            session.bind_route(route)
            await self._async_required(session, state, started, deadline)
            if plan.optional:
                await self._async_optional(session, state, started)
                cleanup_deadline = loop.time() + plan.cleanup_timeout
        except (TimeoutError, asyncio.TimeoutError) as err:
            self._log_response_timeout(state, plan)
            self._log_timeout("BLE transaction", state.stage, started)
            primary_error = GoveeBleClientError(self._timeout_message(state.stage))
            raise primary_error from err
        except asyncio.CancelledError as err:
            primary_error = err
            raise
        except Exception as err:
            primary_error = err
            self._log_failure("BLE transaction", state.stage, started)
            raise
        except BaseException as err:
            primary_error = err
            raise
        finally:
            session.unbind_route(route)
            state.cancel_futures()
            if not session.is_connected:
                state.discard_session = True
            if self._should_stop_notifications(session, state, primary_error):
                await self._async_cleanup_notifications(
                    session, state, started, cleanup_deadline
                )
        return ExchangeResult(tuple(state.frames), state.discard_session)

    async def _async_required(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        started: float,
        deadline: float,
    ) -> None:
        plan = state.plan
        for index, request in enumerate(plan.required, start=1):
            future = asyncio.get_running_loop().create_future()
            state.reset_required(index, future)
            state.stage = f"writing request {index}/{plan.request_count}"
            self._log_stage("BLE transaction", state.stage, started, deadline)
            await session.async_write(request.command, deadline)
            self._debug(
                "%s BLE request %d/%d write completed in %.2f seconds",
                self._log_label,
                index,
                plan.request_count,
                asyncio.get_running_loop().time() - (state.request_started or started),
            )
            state.stage = f"waiting for response {index}/{plan.request_count}"
            self._log_stage("BLE transaction", state.stage, started, deadline)
            frame = await session.async_wait(future, deadline)
            state.frames[index - 1] = frame
            self._log_required_response(state, plan, index)

    async def _async_optional(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        started: float,
    ) -> None:
        state.begin_optional()
        optional_started = asyncio.get_running_loop().time()
        optional_deadline = optional_started + state.plan.optional_timeout
        for offset, request in enumerate(
            state.plan.optional, start=len(state.plan.required) + 1
        ):
            if not await self._async_write_optional(
                session, state, request, offset, started, optional_deadline
            ):
                return
            if state.plan.optional_order is NightLightPollingRequestOrder.SEQUENTIAL:
                if not await self._async_wait_sequential_optional(
                    session, state, offset, optional_started, optional_deadline
                ):
                    break
        await self._async_collect_optional(
            session, state, optional_started, optional_deadline
        )

    async def _async_write_optional(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        request: ExchangeRequest,
        offset: int,
        started: float,
        deadline: float,
    ) -> bool:
        state.initiated_optional_count = offset - len(state.plan.required)
        state.current_request_index = offset
        state.request_started = asyncio.get_running_loop().time()
        state.stage = f"writing optional request {offset}/{state.plan.request_count}"
        self._log_stage("BLE transaction", state.stage, started, deadline)
        try:
            await session.async_write(request.command, deadline)
        except GoveeBleDisconnectedError:
            state.discard_session = True
            self._debug(
                "%s optional BLE telemetry stopped after disconnection",
                self._log_label,
            )
            return False
        except Exception:
            state.discard_session = True
            self._debug(
                "%s optional BLE telemetry write failed; preserving core poll result",
                self._log_label,
                exc_info=True,
            )
            return False
        self._debug(
            "%s optional BLE request %d/%d write completed in %.2f seconds",
            self._log_label,
            offset,
            state.plan.request_count,
            asyncio.get_running_loop().time() - (state.request_started or started),
        )
        return True

    async def _async_wait_sequential_optional(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        offset: int,
        started: float,
        deadline: float,
    ) -> bool:
        state.stage = (
            f"waiting for optional response {offset}/{state.plan.request_count}"
        )
        self._log_stage("BLE transaction", state.stage, started, deadline)
        future = state.optional_futures[offset - len(state.plan.required) - 1]
        try:
            await session.async_wait(future, deadline)
            return True
        except GoveeBleDisconnectedError:
            state.discard_session = True
            self._debug(
                "%s sequential optional BLE telemetry stopped after disconnection",
                self._log_label,
            )
            return False
        except (TimeoutError, asyncio.TimeoutError):
            self._debug(
                "%s sequential optional BLE telemetry stopped after a missing response",
                self._log_label,
            )
            return False

    async def _async_collect_optional(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        started: float,
        deadline: float,
    ) -> None:
        state.stage = "collecting optional responses"
        self._log_stage("BLE transaction", state.stage, started, deadline)
        pending = (
            {future for future in state.optional_futures if not future.done()}
            if state.plan.optional_order is NightLightPollingRequestOrder.PIPELINED
            else set()
        )
        if pending:
            try:
                await session.async_wait(asyncio.gather(*pending), deadline)
            except GoveeBleDisconnectedError:
                state.discard_session = True
                self._debug(
                    "%s optional BLE telemetry stopped after disconnection",
                    self._log_label,
                )
            except (TimeoutError, asyncio.TimeoutError):
                pass
        received_count = sum(
            future.done() and not future.cancelled()
            for future in state.optional_futures
        )
        self._debug(
            "%s optional BLE telemetry received %d/%d responses in %.2f seconds",
            self._log_label,
            received_count,
            len(state.plan.optional),
            asyncio.get_running_loop().time() - started,
        )

    def _log_required_response(
        self, state: _ExchangeState, plan: ExchangePlan, index: int
    ) -> None:
        now = asyncio.get_running_loop().time()
        self._debug(
            "%s BLE response %d/%d received %.2f seconds after write started "
            "(notifications: %d, stale handshakes: %d, nonmatching: %d)",
            self._log_label,
            index,
            plan.request_count,
            now - (state.request_started or now),
            state.notification_count,
            state.ignored_handshake_count,
            state.nonmatching_notification_count,
        )

    def _log_response_timeout(self, state: _ExchangeState, plan: ExchangePlan) -> None:
        if (
            not state.stage.startswith("waiting for response")
            or state.request_started is None
        ):
            return
        now = asyncio.get_running_loop().time()
        self._debug(
            "%s BLE response timeout diagnostic: request %d/%d, %.2f seconds "
            "since write started (notifications: %d, stale handshakes: %d, "
            "nonmatching: %d)",
            self._log_label,
            state.current_request_index,
            plan.request_count,
            now - state.request_started,
            state.notification_count,
            state.ignored_handshake_count,
            state.nonmatching_notification_count,
        )

    @staticmethod
    def _should_stop_notifications(
        session: ExchangeSession,
        state: _ExchangeState,
        primary_error: BaseException | None,
    ) -> bool:
        return (
            not session.persistent_notifications_enabled
            and primary_error is None
            and session.is_connected
            and not state.discard_session
        )

    async def _async_cleanup_notifications(
        self,
        session: ExchangeSession,
        state: _ExchangeState,
        started: float,
        deadline: float,
    ) -> None:
        state.stage = "stopping application notifications"
        self._log_stage("BLE transaction", state.stage, started, deadline)
        try:
            await session.async_stop_notifications(deadline)
        except Exception:
            state.discard_session = True
            self._debug(
                "%s suppressing BLE notification cleanup failure",
                self._log_label,
                exc_info=True,
            )
