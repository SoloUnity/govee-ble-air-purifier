import asyncio
from dataclasses import replace
import logging
import time
from typing import Any

import pytest

from custom_components.govee_ble_air_purifier.bluetooth import client as client_module
from custom_components.govee_ble_air_purifier.bluetooth.client import (
    GoveeBleClient,
    GoveeBleClientError,
    GoveeConnectionArbiter,
    connection_idle_timeout_for_polling_interval,
)
from custom_components.govee_ble_air_purifier.bluetooth import (
    GoveeBleDisconnectedError,
)
from custom_components.govee_ble_air_purifier.bluetooth.framing import build_frame
from custom_components.govee_ble_air_purifier.models import (
    NightLightState,
    PurifierPushUpdate,
    PurifierState,
)
from custom_components.govee_ble_air_purifier.profiles import (
    H7124_PROFILE,
    NightLightPollingCadence,
    NightLightPollingRequestOrder,
    get_profile,
)

NIGHT_LIGHT = H7124_PROFILE.night_light
assert NIGHT_LIGHT is not None


def _pipelined_night_light_with_timeout(timeout: float):
    return replace(
        NIGHT_LIGHT,
        polling=replace(
            NIGHT_LIGHT.polling,
            cadence=NightLightPollingCadence.EVERY_POLL,
            timeout_seconds=timeout,
            request_order=NightLightPollingRequestOrder.PIPELINED,
        ),
    )


class FakeBleakClient:
    def __init__(
        self,
        *,
        fail_disconnect: bool = False,
        fail_stop_notify: bool = False,
        send_extra_on_stop_notify: bool = False,
        send_responses: bool = True,
        stall_start_notify: bool = False,
        stall_write: bool = False,
        stall_stop_notify: bool = False,
        stall_disconnect: bool = False,
        stage_delay: float = 0,
    ) -> None:
        self.fail_disconnect = fail_disconnect
        self.fail_stop_notify = fail_stop_notify
        self.send_extra_on_stop_notify = send_extra_on_stop_notify
        self.send_responses = send_responses
        self.stall_start_notify = stall_start_notify
        self.stall_write = stall_write
        self.stall_stop_notify = stall_stop_notify
        self.stall_disconnect = stall_disconnect
        self.stage_delay = stage_delay
        self.is_connected = True
        self.disconnected = False
        self.disconnect_started = False
        self.notify_handler = None
        self.started_notify: list[str] = []
        self.stopped_notify: list[str] = []
        self.writes: list[tuple[str, bytes, bool]] = []

    async def start_notify(self, char_uuid: str, handler: Any) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_start_notify:
            await asyncio.Event().wait()
        self.started_notify.append(char_uuid)
        self.notify_handler = handler

    async def stop_notify(self, char_uuid: str) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_stop_notify:
            await asyncio.Event().wait()
        if self.send_extra_on_stop_notify and self.notify_handler is not None:
            self.notify_handler(None, build_frame(bytes.fromhex("aa 01 01")))
        self.stopped_notify.append(char_uuid)
        if self.fail_stop_notify:
            raise RuntimeError("cleanup failed")

    async def disconnect(self) -> None:
        await asyncio.sleep(self.stage_delay)
        self.disconnect_started = True
        if self.stall_disconnect:
            await asyncio.Event().wait()
        self.is_connected = False
        self.disconnected = True
        if self.fail_disconnect:
            raise RuntimeError("disconnect failed")

    async def write_gatt_char(
        self, char_uuid: str, command: bytes, *, response: bool
    ) -> None:
        await asyncio.sleep(self.stage_delay)
        if self.stall_write:
            await asyncio.Event().wait()
        self.writes.append((char_uuid, command, response))
        if self.notify_handler is None or not self.send_responses:
            return
        if command == H7124_PROFILE.state_query_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
            )
        if command == H7124_PROFILE.status_query_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55"))
            )
        if command == NIGHT_LIGHT.power_brightness_query_command:
            self.notify_handler(None, build_frame(bytes.fromhex("aa 1b 01 01 64")))
        if command == NIGHT_LIGHT.rgb_state_query_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 1b 05 0d ff 00 00"))
            )
        if command == NIGHT_LIGHT.power_on_command:
            self.notify_handler(None, build_frame(bytes.fromhex("3a 1b 01 01 64")))
        if command == NIGHT_LIGHT.power_off_command:
            self.notify_handler(None, build_frame(bytes.fromhex("3a 1b 01 00 64")))
        if command[:4] == bytes.fromhex("3a 1b 01 02"):
            self.notify_handler(
                None,
                build_frame(bytes.fromhex("3a 1b 01 01") + command[4:5]),
            )
        if command[:4] == bytes.fromhex("3a 1b 05 0d"):
            self.notify_handler(None, command)
        if command == H7124_PROFILE.power_on_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01"))
            )
        if command == H7124_PROFILE.power_off_command:
            self.notify_handler(
                None, build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
            )
        if command in H7124_PROFILE.fan_mode_commands.values():
            self.notify_handler(None, command)


class _TestableGoveeBleClient(GoveeBleClient):
    def __init__(self, fake_client: FakeBleakClient) -> None:
        super().__init__(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
        self.fake_client = fake_client
        self.connection_count = 0

    async def _async_with_connection(
        self, operation: Any, *, deadline: float | None = None
    ) -> Any:
        self.connection_count += 1
        self._client = self.fake_client
        if self._disconnect_signal is None:
            self._disconnect_signal = asyncio.Event()
        return await operation(self.fake_client)


class _RecordingTimeoutClient(GoveeBleClient):
    def __init__(self) -> None:
        super().__init__(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
        self.timeout: float | None = None
        self.optional_timeout: float | None = None
        self.optional_requests: tuple[tuple[bytes, Any], ...] = ()
        self.optional_request_order: Any = None
        self.lease_priority: Any = None

    async def _async_write_and_wait_many(
        self,
        requests: tuple[tuple[bytes, Any], ...],
        *,
        timeout: float = 10.0,
        optional_requests: tuple[tuple[bytes, Any], ...] = (),
        optional_timeout: float = 0.0,
        optional_request_order: Any = None,
        lease_priority: Any = None,
    ) -> tuple[bytes | None, ...]:
        self.timeout = timeout
        self.optional_timeout = optional_timeout
        self.optional_requests = optional_requests
        self.optional_request_order = optional_request_order
        self.lease_priority = lease_priority
        return (
            build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01")),
            build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55")),
            build_frame(bytes.fromhex("aa 1b 01 01 64")),
            build_frame(bytes.fromhex("aa 1b 05 0d ff 00 00")),
        )


class _SlowPreparationClient(_TestableGoveeBleClient):
    async def _async_prepare_connection(self) -> None:
        await asyncio.sleep(0.02)


class _RetryingStateClient(GoveeBleClient):
    def __init__(self, *, always_disconnect: bool = False) -> None:
        super().__init__(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
        self.always_disconnect = always_disconnect
        self.calls = 0

    async def _async_write_and_wait_many(
        self,
        requests: tuple[tuple[bytes, Any], ...],
        *,
        timeout: float = 10.0,
        optional_requests: tuple[tuple[bytes, Any], ...] = (),
        optional_timeout: float = 0.0,
        optional_request_order: Any = None,
        lease_priority: Any = None,
    ) -> tuple[bytes | None, ...]:
        self.calls += 1
        if self.always_disconnect or self.calls == 1:
            raise GoveeBleDisconnectedError("Purifier disconnected")
        return (
            build_frame(bytes.fromhex("aa 01 01 00 81 00 01 01")),
            build_frame(bytes.fromhex("aa 19 81 00 2a 00 00 55")),
            build_frame(bytes.fromhex("aa 1b 01 01 64")),
            build_frame(bytes.fromhex("aa 1b 05 0d ff 00 00")),
        )


class _BestEffortNightLightFake(FakeBleakClient):
    def __init__(self, responses: tuple[str, ...], *, delay: float = 0.0) -> None:
        super().__init__()
        self.responses = responses
        self.delay = delay
        self.response_task: asyncio.Task[None] | None = None

    async def write_gatt_char(
        self, char_uuid: str, command: bytes, *, response: bool
    ) -> None:
        if command not in {
            NIGHT_LIGHT.power_brightness_query_command,
            NIGHT_LIGHT.rgb_state_query_command,
        }:
            await super().write_gatt_char(char_uuid, command, response=response)
            return

        self.writes.append((char_uuid, command, response))
        if command != NIGHT_LIGHT.rgb_state_query_command or not self.responses:
            return

        async def send_responses() -> None:
            if self.delay:
                await asyncio.sleep(self.delay)
            assert self.notify_handler is not None
            for response_name in self.responses:
                frame = (
                    build_frame(bytes.fromhex("aa 1b 01 01 64"))
                    if response_name == "power"
                    else build_frame(bytes.fromhex("aa 1b 05 0d ff 00 00"))
                )
                self.notify_handler(None, frame)

        self.response_task = asyncio.create_task(send_responses())


async def _cancellation_resistant_operation(
    started: asyncio.Event,
    cancellation_seen: asyncio.Event,
    release: asyncio.Event,
    finished: asyncio.Event,
    *,
    late_error: Exception | None = None,
) -> None:
    started.set()
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        cancellation_seen.set()
        await release.wait()
        if late_error is not None:
            raise late_error from None
    finally:
        finished.set()


async def _release_resistant_operation(
    release: asyncio.Event,
    finished: asyncio.Event,
    waiter: asyncio.Task[Any],
    *operations: asyncio.Future[Any],
) -> None:
    release.set()
    await asyncio.wait_for(finished.wait(), 0.5)
    await asyncio.gather(waiter, *operations, return_exceptions=True)
    await asyncio.sleep(0)


def _resistant_operation_events() -> tuple[
    asyncio.Event, asyncio.Event, asyncio.Event, asyncio.Event
]:
    return asyncio.Event(), asyncio.Event(), asyncio.Event(), asyncio.Event()


@pytest.mark.asyncio
async def test_wait_expired_deadline_detaches_precreated_resistant_task() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    started, cancellation_seen, release, finished = _resistant_operation_events()
    disconnect_signal = asyncio.Event()
    operation = asyncio.create_task(
        _cancellation_resistant_operation(started, cancellation_seen, release, finished)
    )
    await asyncio.wait_for(started.wait(), 0.5)
    waiter = asyncio.create_task(
        client._async_wait_for_connection(
            operation,
            disconnect_signal,
            asyncio.get_running_loop().time(),
        )
    )

    try:
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((waiter,), timeout=0.5)
        assert waiter in done
        assert finished.is_set() is False
        with pytest.raises(TimeoutError):
            await waiter
    finally:
        await _release_resistant_operation(release, finished, waiter, operation)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_expired_connection_wait_does_not_start_new_operation() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    started = asyncio.Event()

    async def operation() -> None:
        started.set()

    with pytest.raises(TimeoutError):
        await client._async_wait_for_connection(
            operation(),
            None,
            asyncio.get_running_loop().time(),
        )
    await asyncio.gather(
        *tuple(client_module._ABANDONED_OPERATION_FUTURES),
        return_exceptions=True,
    )
    await asyncio.sleep(0)

    assert started.is_set() is False
    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_connection_wait_timeout_without_disconnect_signal_is_bounded() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    started, cancellation_seen, release, finished = _resistant_operation_events()
    deadline = asyncio.get_running_loop().time() + 0.01
    waiter = asyncio.create_task(
        client._async_wait_for_connection(
            _cancellation_resistant_operation(
                started, cancellation_seen, release, finished
            ),
            None,
            deadline,
        )
    )

    try:
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((waiter,), timeout=0.5)
        assert waiter in done
        assert finished.is_set() is False
        with pytest.raises(TimeoutError):
            await waiter
    finally:
        await _release_resistant_operation(release, finished, waiter)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_connection_wait_disconnect_detaches_and_observes_late_failure() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    started, cancellation_seen, release, finished = _resistant_operation_events()
    disconnect_signal = asyncio.Event()
    loop = asyncio.get_running_loop()
    unhandled: list[dict[str, Any]] = []
    previous_exception_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
    waiter = asyncio.create_task(
        client._async_wait_for_connection(
            _cancellation_resistant_operation(
                started,
                cancellation_seen,
                release,
                finished,
                late_error=RuntimeError("late BLE failure"),
            ),
            disconnect_signal,
            loop.time() + 10,
        )
    )

    try:
        await asyncio.wait_for(started.wait(), 0.5)
        disconnect_signal.set()
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((waiter,), timeout=0.5)
        assert waiter in done
        assert finished.is_set() is False
        with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
            await waiter
    finally:
        await _release_resistant_operation(release, finished, waiter)
        loop.set_exception_handler(previous_exception_handler)

    assert not client_module._ABANDONED_OPERATION_FUTURES
    assert unhandled == []


@pytest.mark.asyncio
async def test_wait_caller_cancel_propagates_before_inner_exit() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    started, cancellation_seen, release, finished = _resistant_operation_events()
    disconnect_signal = asyncio.Event()
    waiter = asyncio.create_task(
        client._async_wait_for_connection(
            _cancellation_resistant_operation(
                started, cancellation_seen, release, finished
            ),
            disconnect_signal,
            asyncio.get_running_loop().time() + 10,
        )
    )

    try:
        await asyncio.wait_for(started.wait(), 0.5)
        waiter.cancel()
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((waiter,), timeout=0.5)
        assert waiter in done
        assert finished.is_set() is False
        with pytest.raises(asyncio.CancelledError):
            await waiter
    finally:
        await _release_resistant_operation(release, finished, waiter)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_connection_wait_disconnect_wins_simultaneous_completion() -> None:
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    operation = asyncio.get_running_loop().create_future()
    operation.set_result("completed")
    disconnect_signal = asyncio.Event()
    disconnect_signal.set()

    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await client._async_wait_for_connection(
            operation,
            disconnect_signal,
            asyncio.get_running_loop().time() + 10,
        )


@pytest.mark.asyncio
async def test_connection_wait_disconnect_signal_wins_before_waiter_finishes() -> None:
    blocked = asyncio.Event()

    class DelayedDisconnectSignal(asyncio.Event):
        async def wait(self) -> bool:
            await super().wait()
            await blocked.wait()
            return True

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    disconnect_signal = DelayedDisconnectSignal()

    async def operation() -> str:
        disconnect_signal.set()
        return "completed"

    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await client._async_wait_for_connection(
            operation(),
            disconnect_signal,
            asyncio.get_running_loop().time() + 10,
        )
    await asyncio.gather(
        *tuple(client_module._ABANDONED_OPERATION_FUTURES),
        return_exceptions=True,
    )
    await asyncio.sleep(0)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_failed_transaction_quarantines_old_operation_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    started, cancellation_seen, release, finished = _resistant_operation_events()

    class CancellationResistantClient(FakeBleakClient):
        async def start_notify(self, char_uuid: str, handler: Any) -> None:
            await _cancellation_resistant_operation(
                started,
                cancellation_seen,
                release,
                finished,
                late_error=RuntimeError("late start-notify failure"),
            )

    first = CancellationResistantClient()
    second = FakeBleakClient()
    clients = iter((first, second))
    establish_count = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        establish_count += 1
        return next(clients)

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client = GoveeBleClient(
        None,
        "AA:BB:CC:DD:EE:FF",
        connection_arbiter=GoveeConnectionArbiter(),
    )
    transaction = asyncio.create_task(
        client._async_write_and_wait(
            H7124_PROFILE.power_on_command,
            H7124_PROFILE.is_power_state_response,
            timeout=0.01,
        )
    )
    close_task: asyncio.Task[Any] | None = None

    try:
        await asyncio.wait_for(cancellation_seen.wait(), 0.5)
        done, _pending = await asyncio.wait((transaction,), timeout=0.5)
        assert transaction in done
        with pytest.raises(
            GoveeBleClientError, match="Timed out starting purifier notifications"
        ):
            await transaction

        assert finished.is_set() is False
        assert first.disconnected is True
        assert first.stopped_notify == []
        assert client._client is None
        assert client._disconnect_signal is None
        assert client._lock.locked() is False
        assert client_module._ABANDONED_OPERATION_FUTURES
        assert client.diagnostics()["quarantined_operation_count"] == 1

        assert await client.async_get_state() == PurifierState(
            is_on=True,
            pm25=42,
            filter_life=85,
            night_light=NightLightState(
                is_on=True,
                brightness_percent=100,
                rgb_color=(255, 0, 0),
            ),
        )
        assert establish_count == 2
        assert client._client is second

        close_task = asyncio.create_task(client.async_close())
        close_done, _pending = await asyncio.wait((close_task,), timeout=0.5)
        assert close_task in close_done
        await close_task
    finally:
        release.set()
        await asyncio.wait_for(finished.wait(), 0.5)
        await asyncio.gather(transaction, return_exceptions=True)
        if close_task is not None:
            await asyncio.gather(close_task, return_exceptions=True)
        await asyncio.sleep(0)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
async def test_first_h7124_poll_reconciles_night_light_in_one_subscription() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 0, 0),
        ),
    )

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == []
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.state_query_command, False),
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.status_query_command, False),
        (
            H7124_PROFILE.write_char_uuid,
            NIGHT_LIGHT.power_brightness_query_command,
            False,
        ),
        (
            H7124_PROFILE.write_char_uuid,
            NIGHT_LIGHT.rgb_state_query_command,
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_h7124_reconciles_light_once_per_periodic_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)
    now = 1000.0
    monkeypatch.setattr(client, "_monotonic", lambda: now)

    assert (await client.async_get_state()).night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(255, 0, 0),
    )
    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )
    now = 1300.0
    assert (await client.async_get_state()).night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(255, 0, 0),
    )
    assert [write[1] for write in fake.writes] == [
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
        NIGHT_LIGHT.power_brightness_query_command,
        NIGHT_LIGHT.rgb_state_query_command,
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
        NIGHT_LIGHT.power_brightness_query_command,
        NIGHT_LIGHT.rgb_state_query_command,
    ]
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == []


@pytest.mark.asyncio
async def test_h7124_waits_for_first_light_response_before_second_query() -> None:
    class ResponsePacedLightFake(FakeBleakClient):
        def __init__(self) -> None:
            super().__init__()
            self.power_query_written = asyncio.Event()
            self.release_power_response = asyncio.Event()
            self.power_response_sent = False
            self.response_task: asyncio.Task[None] | None = None

        async def write_gatt_char(
            self, char_uuid: str, command: bytes, *, response: bool
        ) -> None:
            if command == NIGHT_LIGHT.power_brightness_query_command:
                self.writes.append((char_uuid, command, response))
                self.power_query_written.set()

                async def respond_later() -> None:
                    await self.release_power_response.wait()
                    self.power_response_sent = True
                    assert self.notify_handler is not None
                    self.notify_handler(
                        None, build_frame(bytes.fromhex("aa 1b 01 01 64"))
                    )

                self.response_task = asyncio.create_task(respond_later())
                return
            if command == NIGHT_LIGHT.rgb_state_query_command:
                assert self.power_response_sent is True
            await super().write_gatt_char(char_uuid, command, response=response)

    fake = ResponsePacedLightFake()
    client = _TestableGoveeBleClient(fake)
    poll = asyncio.create_task(client.async_get_state())
    await asyncio.wait_for(fake.power_query_written.wait(), 0.1)

    assert NIGHT_LIGHT.rgb_state_query_command not in [write[1] for write in fake.writes]
    fake.release_power_response.set()
    assert (await asyncio.wait_for(poll, 0.1)).night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(255, 0, 0),
    )
    assert fake.response_task is not None
    await fake.response_task


@pytest.mark.asyncio
async def test_h7124_missing_light_response_backs_off_without_failing_core_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _BestEffortNightLightFake(())
    client = _TestableGoveeBleClient(fake)
    client._profile = replace(
        H7124_PROFILE,
        night_light=replace(
            NIGHT_LIGHT,
            polling=replace(NIGHT_LIGHT.polling, timeout_seconds=0.01),
        ),
    )
    now = 1000.0
    monkeypatch.setattr(client, "_monotonic", lambda: now)

    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )
    diagnostics = client.diagnostics()["night_light_polling"]
    assert diagnostics["attempt_count"] == 1
    assert diagnostics["missed_count"] == 1
    assert diagnostics["consecutive_failures"] == 1
    assert diagnostics["next_attempt_in_seconds"] == 600
    assert [write[1] for write in fake.writes] == [
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
        NIGHT_LIGHT.power_brightness_query_command,
    ]

    now = 1300.0
    assert await client.async_get_state() == PurifierState(
        is_on=True, pm25=42, filter_life=85
    )
    assert client.diagnostics()["night_light_polling"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_persistent_listener_routes_idle_h7124_physical_pushes() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)
    updates: list[PurifierPushUpdate] = []
    client.set_push_callback(updates.append)

    await client.async_get_state()
    await client.async_set_power(True)

    assert updates == []
    assert fake.notify_handler is not None
    fake.notify_handler(
        None, build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
    )
    low_command = H7124_PROFILE.fan_mode_commands["Low"]
    fake.notify_handler(None, build_frame(bytes((0xEE,)) + low_command[1:19]))
    fake.notify_handler(None, build_frame(bytes.fromhex("ee 1b 01 01 32")))
    h7129_auto = get_profile("h7129").fan_mode_commands["Auto"]
    fake.notify_handler(None, build_frame(bytes((0xEE,)) + h7129_auto[1:19]))

    assert updates == [
        PurifierPushUpdate(is_on=False),
        PurifierPushUpdate(fan_mode="Low"),
        PurifierPushUpdate(
            night_light=NightLightState(is_on=True, brightness_percent=50)
        ),
    ]
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == []
    assert client.diagnostics()["push_counts"] == {
        "power": 1,
        "fan_mode": 1,
        "night_light": 1,
    }
    assert client.diagnostics()["ignored_push_count"] == 1


@pytest.mark.asyncio
async def test_unsolicited_push_during_unrelated_transaction_is_published() -> None:
    updates: list[PurifierPushUpdate] = []

    class PushBeforeResponseFake(FakeBleakClient):
        async def write_gatt_char(
            self, char_uuid: str, command: bytes, *, response: bool
        ) -> None:
            assert self.notify_handler is not None
            sleep_command = H7124_PROFILE.fan_mode_commands["Sleep"]
            self.notify_handler(
                None, build_frame(bytes((0xEE,)) + sleep_command[1:19])
            )
            await super().write_gatt_char(char_uuid, command, response=response)

    fake = PushBeforeResponseFake()
    client = _TestableGoveeBleClient(fake)
    client.set_push_callback(updates.append)

    assert await client.async_set_power(True) is True
    assert updates == [PurifierPushUpdate(fan_mode="Sleep")]


@pytest.mark.asyncio
async def test_stale_persistent_listener_callback_cannot_publish() -> None:
    first = FakeBleakClient()
    client = _TestableGoveeBleClient(first)
    updates: list[PurifierPushUpdate] = []
    client.set_push_callback(updates.append)
    await client.async_get_state()
    stale_handler = first.notify_handler
    assert stale_handler is not None

    client._clear_connection_state()
    second = FakeBleakClient()
    client.fake_client = second
    await client.async_get_state()
    assert second.notify_handler is not None

    power_off = build_frame(bytes.fromhex("aa 01 00 00 81 00 01 01"))
    stale_handler(None, power_off)
    second.notify_handler(None, power_off)

    assert updates == [PurifierPushUpdate(is_on=False)]


@pytest.mark.asyncio
async def test_malformed_idle_notification_drops_persistent_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = FakeBleakClient()
    disconnects: list[Any] = []

    async def async_establish_connection(*args: Any, **kwargs: Any) -> FakeBleakClient:
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append(passed_client)
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    await client.async_get_state()
    assert fake.notify_handler is not None

    fake.notify_handler(None, bytes(19) + b"\x01")
    for _ in range(10):
        if disconnects:
            break
        await asyncio.sleep(0)

    assert disconnects == [fake]
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]
    assert client._client is None


@pytest.mark.asyncio
async def test_stalled_notification_cleanup_does_not_poison_reconnection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A listener cleanup that ignores cancellation stays on the old client."""

    from custom_components.govee_ble_air_purifier.bluetooth import transport

    stop_started, cancellation_seen, release, stop_finished = (
        _resistant_operation_events()
    )

    class CancellationResistantStopClient(FakeBleakClient):
        async def stop_notify(self, char_uuid: str) -> None:
            await _cancellation_resistant_operation(
                stop_started,
                cancellation_seen,
                release,
                stop_finished,
            )

    first = CancellationResistantStopClient()
    second = FakeBleakClient()
    clients = iter((first, second))
    establish_count = 0

    async def async_establish_connection(*args: Any, **kwargs: Any) -> FakeBleakClient:
        nonlocal establish_count
        establish_count += 1
        return next(clients)

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT", 0.01)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)

    try:
        await client.async_get_state()
        assert first.notify_handler is not None
        first.notify_handler(None, bytes(19) + b"\x01")

        await asyncio.wait_for(cancellation_seen.wait(), timeout=1)
        for _ in range(20):
            recovery = client._notification_recovery_task
            if recovery is None or recovery.done():
                break
            await asyncio.sleep(0)

        assert first.disconnected is True
        assert client._client is None
        assert client.diagnostics()["quarantined_operation_count"] == 1
        assert client.diagnostics()["notification_recovery_active"] is False

        state = await client.async_get_state()
        assert state.is_on is True
        assert state.pm25 == 42
        assert establish_count == 2
        assert client._client is second
    finally:
        release.set()
        await asyncio.wait_for(stop_finished.wait(), timeout=1)
        await client.async_close()
        await asyncio.sleep(0)

    assert not client_module._ABANDONED_OPERATION_FUTURES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("responses", "expected_night_light"),
    [
        ((), None),
        (
            ("power",),
            NightLightState(is_on=True, brightness_percent=100),
        ),
        (
            ("rgb",),
            NightLightState(rgb_color=(255, 0, 0)),
        ),
        (
            ("rgb", "power"),
            NightLightState(
                is_on=True,
                brightness_percent=100,
                rgb_color=(255, 0, 0),
            ),
        ),
    ],
)
async def test_get_state_preserves_core_state_with_best_effort_light_responses(
    responses: tuple[str, ...],
    expected_night_light: NightLightState | None,
) -> None:
    fake = _BestEffortNightLightFake(responses)
    client = _TestableGoveeBleClient(fake)
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.01),
    )

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=expected_night_light,
    )
    assert [write[1] for write in fake.writes] == [
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
        NIGHT_LIGHT.power_brightness_query_command,
        NIGHT_LIGHT.rgb_state_query_command,
    ]
    if fake.response_task is not None:
        await fake.response_task


@pytest.mark.asyncio
async def test_get_state_collects_delayed_pipelined_light_responses(
) -> None:
    fake = _BestEffortNightLightFake(("rgb", "power"), delay=0.01)
    client = _TestableGoveeBleClient(fake)
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.05),
    )

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 0, 0),
        ),
    )
    assert fake.response_task is not None
    await fake.response_task


@pytest.mark.asyncio
async def test_optional_response_is_not_matched_before_its_query() -> None:
    class EarlyRgbFake(FakeBleakClient):
        async def write_gatt_char(
            self, char_uuid: str, command: bytes, *, response: bool
        ) -> None:
            if command == NIGHT_LIGHT.power_brightness_query_command:
                self.writes.append((char_uuid, command, response))
                assert self.notify_handler is not None
                self.notify_handler(
                    None, build_frame(bytes.fromhex("aa 1b 05 0d ff 00 00"))
                )
                self.notify_handler(
                    None, build_frame(bytes.fromhex("aa 1b 01 01 64"))
                )
                return
            if command == NIGHT_LIGHT.rgb_state_query_command:
                self.writes.append((char_uuid, command, response))
                assert self.notify_handler is not None
                self.notify_handler(
                    None, build_frame(bytes.fromhex("aa 1b 05 0d 00 00 ff"))
                )
                return
            await super().write_gatt_char(char_uuid, command, response=response)

    client = _TestableGoveeBleClient(EarlyRgbFake())
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.01),
    )

    assert (await client.async_get_state()).night_light == NightLightState(
        is_on=True,
        brightness_percent=100,
        rgb_color=(0, 0, 255),
    )


@pytest.mark.asyncio
async def test_missing_light_telemetry_keeps_healthy_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = _BestEffortNightLightFake(())
    disconnects: list[Any] = []

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append(passed_client)
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.01),
    )

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )
    assert client._client is fake
    assert client._fresh_advertisement_after is None
    assert disconnects == []

    await client.async_close()
    assert disconnects == [fake]


@pytest.mark.asyncio
async def test_optional_write_failure_preserves_core_state_and_drops_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    class FailingLightWriteFake(FakeBleakClient):
        async def write_gatt_char(
            self, char_uuid: str, command: bytes, *, response: bool
        ) -> None:
            if command == NIGHT_LIGHT.power_brightness_query_command:
                raise RuntimeError("light query failed")
            await super().write_gatt_char(char_uuid, command, response=response)

    fake = FailingLightWriteFake()
    disconnects: list[Any] = []

    async def async_establish_connection(*args: Any, **kwargs: Any) -> FakeBleakClient:
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append(passed_client)
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.01),
    )

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]
    assert disconnects == [fake]
    assert client._client is None


@pytest.mark.asyncio
async def test_disconnect_wakes_optional_collection_and_preserves_core_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    class DisconnectingLightFake(_BestEffortNightLightFake):
        def __init__(self) -> None:
            super().__init__(())
            self.disconnect_callback: Any = None
            self.disconnect_task: asyncio.Task[None] | None = None

        async def write_gatt_char(
            self, char_uuid: str, command: bytes, *, response: bool
        ) -> None:
            await super().write_gatt_char(char_uuid, command, response=response)
            if command != NIGHT_LIGHT.rgb_state_query_command:
                return

            async def disconnect_later() -> None:
                await asyncio.sleep(0.01)
                self.is_connected = False
                self.disconnect_callback(self)

            self.disconnect_task = asyncio.create_task(disconnect_later())

    fake = DisconnectingLightFake()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        fake.disconnect_callback = disconnected_callback
        return fake

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    client._profile = replace(
        H7124_PROFILE,
        night_light=_pipelined_night_light_with_timeout(0.2),
    )
    started = asyncio.get_running_loop().time()

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )
    assert asyncio.get_running_loop().time() - started < 0.1
    assert client._client is None
    assert client._fresh_advertisement_after is not None
    assert fake.disconnect_task is not None
    await fake.disconnect_task


@pytest.mark.asyncio
async def test_get_state_skips_night_light_queries_without_profile_capability() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)
    client._profile = get_profile("h7126")

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
    )
    assert [write[1] for write in fake.writes] == [
        H7124_PROFILE.state_query_command,
        H7124_PROFILE.status_query_command,
    ]


@pytest.mark.asyncio
async def test_get_state_uses_shorter_poll_timeout() -> None:
    client = _RecordingTimeoutClient()

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 0, 0),
        ),
    )
    assert client.timeout == 5.0
    assert [request[0] for request in client.optional_requests] == [
        NIGHT_LIGHT.power_brightness_query_command,
        NIGHT_LIGHT.rgb_state_query_command,
    ]
    assert client.optional_timeout == 1
    assert (
        client.optional_request_order is NightLightPollingRequestOrder.SEQUENTIAL
    )
    assert client.lease_priority is client_module._ConnectionLeasePriority.POLL


@pytest.mark.asyncio
async def test_get_state_retries_once_after_a_disconnect() -> None:
    client = _RetryingStateClient()

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 0, 0),
        ),
    )
    assert client.calls == 2


@pytest.mark.asyncio
async def test_get_state_does_not_retry_a_second_disconnect() -> None:
    client = _RetryingStateClient(always_disconnect=True)

    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await client.async_get_state()

    assert client.calls == 2


@pytest.mark.asyncio
async def test_commands_do_not_replay_after_a_disconnect() -> None:
    client = _RetryingStateClient(always_disconnect=True)

    with pytest.raises(GoveeBleDisconnectedError, match="disconnected"):
        await client.async_set_power(True)

    assert client.calls == 1


@pytest.mark.asyncio
async def test_stop_notify_cleanup_error_does_not_mask_timeout() -> None:
    fake = FakeBleakClient(fail_stop_notify=True, send_responses=False)
    client = _TestableGoveeBleClient(fake)

    with pytest.raises(
        GoveeBleClientError, match="Timed out waiting for purifier response"
    ):
        await client._async_write_and_wait(
            H7124_PROFILE.status_query_command,
            H7124_PROFILE.is_status_response,
            timeout=0.01,
        )
    assert len(fake.writes) == 1


@pytest.mark.asyncio
async def test_stop_notify_cleanup_error_does_not_fail_successful_command() -> None:
    fake = FakeBleakClient(fail_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_power(True) is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stall", "message"),
    [
        ("stall_start_notify", "Timed out starting purifier notifications"),
        ("stall_write", "Timed out writing purifier request"),
    ],
)
async def test_notification_transaction_stages_are_bounded(
    stall: str, message: str
) -> None:
    fake = FakeBleakClient(**{stall: True})
    client = _TestableGoveeBleClient(fake)

    with pytest.raises(GoveeBleClientError, match=message):
        await asyncio.wait_for(
            client._async_write_and_wait(
                H7124_PROFILE.power_on_command,
                H7124_PROFILE.is_power_state_response,
                timeout=0.01,
            ),
            0.1,
        )


@pytest.mark.asyncio
async def test_notification_transaction_uses_one_timeout_budget() -> None:
    fake = FakeBleakClient(stage_delay=0.02)
    client = _TestableGoveeBleClient(fake)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with pytest.raises(GoveeBleClientError, match="Timed out"):
        await client._async_write_and_wait(
            H7124_PROFILE.power_on_command,
            H7124_PROFILE.is_power_state_response,
            timeout=0.03,
        )

    assert loop.time() - started < 0.05


@pytest.mark.asyncio
async def test_connection_preparation_does_not_consume_transaction_timeout() -> None:
    client = _SlowPreparationClient(FakeBleakClient())

    assert await client._async_write_and_wait(
        H7124_PROFILE.power_on_command,
        H7124_PROFILE.is_power_state_response,
        timeout=0.01,
    )


@pytest.mark.asyncio
async def test_connection_establishment_does_not_consume_transaction_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = FakeBleakClient()
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        await asyncio.sleep(0.02)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client._async_write_and_wait(
        H7124_PROFILE.power_on_command,
        H7124_PROFILE.is_power_state_response,
        timeout=0.01,
    )

    await client.async_close()


@pytest.mark.asyncio
async def test_connection_establishment_uses_its_own_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = FakeBleakClient()
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    deadlines: list[float] = []

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        deadlines.append(deadline)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(transport, "CONNECTION_TIMEOUT", 7.0, raising=False)
    loop = asyncio.get_running_loop()
    before = loop.time()

    await client._async_write_and_wait(
        H7124_PROFILE.power_on_command,
        H7124_PROFILE.is_power_state_response,
        timeout=0.01,
    )

    after = loop.time()
    assert len(deadlines) == 1
    assert before + 7.0 <= deadlines[0] <= after + 7.0

    await client.async_close()


@pytest.mark.asyncio
async def test_idle_cleanup_finishes_before_connection_preparation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _TestableGoveeBleClient(FakeBleakClient())
    events: list[str] = []

    async def async_wait_for_idle_disconnect() -> None:
        events.append("idle_cleanup")

    async def async_prepare_connection() -> None:
        events.append("prepare")

    monkeypatch.setattr(
        client, "_async_wait_for_idle_disconnect", async_wait_for_idle_disconnect
    )
    monkeypatch.setattr(client, "_async_prepare_connection", async_prepare_connection)

    assert await client.async_set_power(True) is True

    assert events == ["idle_cleanup", "prepare"]


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["command", "poll"])
async def test_running_idle_cleanup_has_own_timeout_and_prevents_writes(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import (
        client as client_module,
    )

    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)
    release_cleanup = asyncio.Event()
    cleanup_task = asyncio.create_task(release_cleanup.wait())
    client._idle_disconnect_task = cleanup_task
    monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT", 0.01)

    try:
        with pytest.raises(
            GoveeBleClientError,
            match="Timed out waiting for idle disconnect cleanup",
        ):
            if operation == "command":
                await asyncio.wait_for(client.async_set_power(True), 0.1)
            else:
                await asyncio.wait_for(client.async_get_state(), 0.1)
    finally:
        release_cleanup.set()
        await cleanup_task
        client._idle_disconnect_task = None

    assert fake.started_notify == []
    assert fake.writes == []


@pytest.mark.asyncio
async def test_close_during_connection_drops_late_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import (
        client as client_module,
    )
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = FakeBleakClient()
    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF", profile=H7124_PROFILE)
    connection_started = asyncio.Event()
    allow_connection = asyncio.Event()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        connection_started.set()
        await allow_connection.wait()
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(client_module, "DEFAULT_TIMEOUT", 0.01)
    monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT", 0.01)

    operation = asyncio.create_task(client.async_get_state())
    await connection_started.wait()
    await client.async_close()
    allow_connection.set()
    result = (await asyncio.gather(operation, return_exceptions=True))[0]

    assert isinstance(result, GoveeBleClientError)
    assert "closed" in str(result)
    assert client._client is None
    assert fake.disconnected is True


@pytest.mark.asyncio
async def test_notification_transaction_timeout_includes_lock_wait() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)
    await client._lock.acquire()
    try:
        with pytest.raises(GoveeBleClientError, match="transaction lock"):
            await asyncio.wait_for(
                client._async_write_and_wait(
                    H7124_PROFILE.power_on_command,
                    H7124_PROFILE.is_power_state_response,
                    timeout=0.01,
                ),
                0.1,
            )
    finally:
        client._lock.release()
    assert fake.writes == []


@pytest.mark.asyncio
async def test_stalled_stop_notify_is_bounded_without_failing_success() -> None:
    fake = FakeBleakClient(stall_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await asyncio.wait_for(
        client._async_write_and_wait(
            H7124_PROFILE.power_on_command,
            H7124_PROFILE.is_power_state_response,
            timeout=0.01,
        ),
        0.1,
    )


@pytest.mark.asyncio
async def test_extra_notification_after_batch_completion_is_ignored() -> None:
    fake = FakeBleakClient(send_extra_on_stop_notify=True)
    client = _TestableGoveeBleClient(fake)

    assert await client.async_get_state() == PurifierState(
        is_on=True,
        pm25=42,
        filter_life=85,
        night_light=NightLightState(
            is_on=True,
            brightness_percent=100,
            rgb_color=(255, 0, 0),
        ),
    )


@pytest.mark.asyncio
async def test_night_light_commands_use_profile_frames_and_confirmations() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_night_light_power(True) == NightLightState(
        is_on=True, brightness_percent=100
    )
    assert await client.async_set_night_light_brightness(50) == NightLightState(
        is_on=True, brightness_percent=50
    )
    assert await client.async_set_night_light_rgb((255, 255, 0)) == NightLightState(
        rgb_color=(255, 255, 0)
    )
    assert await client.async_set_night_light_power(False) == NightLightState(
        is_on=False, brightness_percent=100
    )

    assert [write[1] for write in fake.writes] == [
        NIGHT_LIGHT.power_on_command,
        NIGHT_LIGHT.build_brightness_command(50),
        NIGHT_LIGHT.build_rgb_command((255, 255, 0)),
        NIGHT_LIGHT.power_off_command,
    ]


@pytest.mark.asyncio
async def test_power_and_mode_command_is_batched_in_one_connection() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    await client.async_set_power_and_fan_mode("Sleep")

    assert client.connection_count == 1
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.power_on_command, False),
        (
            H7124_PROFILE.write_char_uuid,
            H7124_PROFILE.fan_mode_commands["Sleep"],
            False,
        ),
    ]


@pytest.mark.asyncio
async def test_power_command_waits_for_aa01_confirmation() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_power(True) is True

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == []
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.power_on_command, False),
    ]


@pytest.mark.asyncio
async def test_fan_mode_command_waits_for_exact_echo_confirmation() -> None:
    fake = FakeBleakClient()
    client = _TestableGoveeBleClient(fake)

    assert await client.async_set_fan_mode("Low") == "Low"

    assert client.connection_count == 1
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    assert fake.stopped_notify == []
    assert fake.writes == [
        (H7124_PROFILE.write_char_uuid, H7124_PROFILE.fan_mode_commands["Low"], False),
    ]


@pytest.mark.asyncio
async def test_connection_is_established_once_and_reused(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    hass = object()
    client = GoveeBleClient(hass, "AA:BB:CC:DD:EE:FF")
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )
    fake = FakeBleakClient()
    callbacks: list[Any] = []
    calls: list[tuple[Any, str, float]] = []
    disconnects: list[tuple[Any, float]] = []

    async def operation(passed_client: Any) -> str:
        assert passed_client is fake
        return "result"

    async def async_establish_connection(
        passed_hass: Any,
        address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        calls.append((passed_hass, address, deadline))
        callbacks.append(disconnected_callback)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append((passed_client, deadline))
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    deadline = asyncio.get_running_loop().time() + 4.0

    assert await client._async_with_connection(operation, deadline=deadline) == "result"
    assert await client._async_with_connection(operation, deadline=deadline) == "result"

    assert calls == [(hass, "AA:BB:CC:DD:EE:FF", deadline)]
    assert len(callbacks) == 1
    assert disconnects == []

    await client.async_close()

    assert len(disconnects) == 1
    assert disconnects[0][0] is fake
    assert (
        f"{client._log_label} retaining dedicated BLE connection for push notifications"
        in caplog.text
    )
    assert (
        f"{client._log_label} BLE client closing (cached connection: True)"
        in caplog.text
    )
    assert f"{client._log_label} releasing cached BLE connection" in caplog.text
    assert f"{client._log_label} BLE client closed" in caplog.text


@pytest.mark.asyncio
async def test_push_enabled_dedicated_connection_ignores_long_poll_idle_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    fake = FakeBleakClient()

    async def async_establish_connection(*args: Any, **kwargs: Any) -> FakeBleakClient:
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client = GoveeBleClient(
        None,
        "AA:BB:CC:DD:EE:FF",
        profile=H7124_PROFILE,
        polling_interval_seconds=300,
    )

    await client.async_get_state()

    assert client._client is fake
    assert client._idle_disconnect_handle is None
    assert fake.started_notify == [H7124_PROFILE.notify_char_uuid]
    await client.async_close()
    assert fake.stopped_notify == [H7124_PROFILE.notify_char_uuid]


@pytest.mark.asyncio
async def test_same_model_log_labels_are_distinct_and_do_not_expose_addresses(
    caplog: pytest.LogCaptureFixture,
) -> None:
    first = GoveeBleClient(None, "AA:BB:CC:DD:EE:01")
    second = GoveeBleClient(None, "AA:BB:CC:DD:EE:02")
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )

    first._schedule_idle_disconnect()
    second._schedule_idle_disconnect()
    first._cancel_idle_disconnect()
    second._cancel_idle_disconnect()

    assert first._log_label != second._log_label
    assert first._log_label in caplog.text
    assert second._log_label in caplog.text
    assert "AA:BB:CC:DD:EE:01" not in caplog.text
    assert "AA:BB:CC:DD:EE:02" not in caplog.text


@pytest.mark.parametrize(
    ("polling_interval", "expected_timeout"),
    [
        (5, 10.0),
        (10, 15.0),
        (20, 25.0),
        (25, 30.0),
        (30, 5.0),
        (60, 5.0),
        (300, 5.0),
    ],
)
def test_connection_idle_timeout_adapts_to_polling_interval(
    polling_interval: int, expected_timeout: float
) -> None:
    assert (
        connection_idle_timeout_for_polling_interval(polling_interval)
        == expected_timeout
    )


def test_client_uses_profile_polling_interval_for_idle_timeout() -> None:
    client = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:FF", profile=get_profile("h7129")
    )

    assert client._connection_idle_timeout == 8.0


@pytest.mark.asyncio
async def test_connection_arbiter_shares_one_slot_across_four_purifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    arbiter = GoveeConnectionArbiter()
    clients = [
        GoveeBleClient(
            None,
            f"AA:BB:CC:DD:EE:{index:02X}",
            connection_arbiter=arbiter,
        )
        for index in range(4)
    ]
    active: set[FakeBleakClient] = set()
    peak_active = 0
    connected_by_address: dict[str, list[FakeBleakClient]] = {}

    async def async_establish_connection(
        _hass: Any,
        address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal peak_active
        if active:
            raise GoveeBleClientError("Bluetooth proxy has no free connection slots")
        connected = FakeBleakClient()
        active.add(connected)
        peak_active = max(peak_active, len(active))
        connected_by_address.setdefault(address, []).append(connected)
        return connected

    async def async_disconnect(passed_client: FakeBleakClient, *, deadline: float) -> None:
        await passed_client.disconnect()
        active.discard(passed_client)

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    for _round in range(2):
        states = await asyncio.gather(*(client.async_get_state() for client in clients))
        assert all(state.is_on is True for state in states)
        assert len(active) == 1

    assert peak_active == 1
    assert all(len(connections) == 2 for connections in connected_by_address.values())
    for connections in connected_by_address.values():
        assert [write[1] for write in connections[0].writes] == [
            H7124_PROFILE.state_query_command,
            H7124_PROFILE.status_query_command,
            NIGHT_LIGHT.power_brightness_query_command,
            NIGHT_LIGHT.rgb_state_query_command,
        ]
        assert [write[1] for write in connections[1].writes] == [
            H7124_PROFILE.state_query_command,
            H7124_PROFILE.status_query_command,
        ]

    await asyncio.gather(*(client.async_close() for client in clients))
    assert not active


@pytest.mark.asyncio
async def test_two_dedicated_and_two_shared_purifiers_use_three_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only opted-in purifiers rotate through the shared connection slot."""

    from custom_components.govee_ble_air_purifier.bluetooth import transport

    arbiter = GoveeConnectionArbiter()
    dedicated = [
        GoveeBleClient(None, f"AA:BB:CC:DD:EE:{index:02X}")
        for index in range(2)
    ]
    shared = [
        GoveeBleClient(
            None,
            f"AA:BB:CC:DD:EE:{index:02X}",
            connection_arbiter=arbiter,
        )
        for index in range(2, 4)
    ]
    active: set[FakeBleakClient] = set()
    peak_active = 0
    established_by_address: dict[str, int] = {}

    async def async_establish_connection(
        _hass: Any,
        address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal peak_active
        if len(active) >= 3:
            raise GoveeBleClientError("Bluetooth proxy has no free connection slots")
        connected = FakeBleakClient()
        active.add(connected)
        peak_active = max(peak_active, len(active))
        established_by_address[address] = established_by_address.get(address, 0) + 1
        return connected

    async def async_disconnect(passed_client: FakeBleakClient, *, deadline: float) -> None:
        await passed_client.disconnect()
        active.discard(passed_client)

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert all(
        state.is_on is True
        for state in await asyncio.gather(
            *(client.async_get_state() for client in dedicated)
        )
    )
    for _round in range(2):
        assert all(
            state.is_on is True
            for state in await asyncio.gather(
                *(client.async_get_state() for client in shared)
            )
        )
        assert len(active) == 3

    assert peak_active == 3
    assert established_by_address == {
        "AA:BB:CC:DD:EE:00": 1,
        "AA:BB:CC:DD:EE:01": 1,
        "AA:BB:CC:DD:EE:02": 2,
        "AA:BB:CC:DD:EE:03": 2,
    }

    await asyncio.gather(*(client.async_close() for client in [*dedicated, *shared]))
    assert not active


@pytest.mark.asyncio
async def test_connection_arbiter_prioritizes_control_over_queued_poll(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user command queued after a poll gets the next available BLE lease."""

    from custom_components.govee_ble_air_purifier.bluetooth import transport

    arbiter = GoveeConnectionArbiter()
    blocker = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:01", connection_arbiter=arbiter
    )
    poller = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:02", connection_arbiter=arbiter
    )
    commander = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:03", connection_arbiter=arbiter
    )
    connection_order: list[str] = []

    async def async_establish_connection(
        _hass: Any,
        address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        connection_order.append(address)
        return FakeBleakClient()

    async def async_disconnect(passed_client: FakeBleakClient, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def hold_connection(_client: Any) -> None:
        blocker_started.set()
        await release_blocker.wait()

    blocker_task = asyncio.create_task(blocker._async_with_connection(hold_connection))
    await blocker_started.wait()
    preparation_order: list[str] = []

    async def prepare_poller() -> None:
        preparation_order.append("poll")

    async def prepare_commander() -> None:
        preparation_order.append("command")

    monkeypatch.setattr(poller, "_async_prepare_connection", prepare_poller)
    monkeypatch.setattr(commander, "_async_prepare_connection", prepare_commander)
    poll_task = asyncio.create_task(poller.async_get_state())
    await asyncio.sleep(0)
    command_task = asyncio.create_task(commander.async_set_power(True))
    await asyncio.sleep(0)

    assert len(arbiter._poll_waiters) == 1
    assert len(arbiter._command_waiters) == 1
    assert preparation_order == []
    release_blocker.set()

    await asyncio.wait_for(blocker_task, timeout=1)
    assert await asyncio.wait_for(command_task, timeout=1) is True
    assert (await asyncio.wait_for(poll_task, timeout=1)).is_on is True
    assert connection_order == [
        "AA:BB:CC:DD:EE:01",
        "AA:BB:CC:DD:EE:03",
        "AA:BB:CC:DD:EE:02",
    ]
    assert preparation_order == ["command", "poll"]

    await asyncio.gather(
        blocker.async_close(), poller.async_close(), commander.async_close()
    )


@pytest.mark.asyncio
async def test_connection_preparation_failure_releases_priority_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed work after priority admission cannot strand later controls."""

    arbiter = GoveeConnectionArbiter()
    failing = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:01", connection_arbiter=arbiter
    )
    following = GoveeBleClient(None, "AA:BB:CC:DD:EE:02")
    following_ran = False

    async def fail_preparation() -> None:
        raise GoveeBleClientError("preparation failed")

    async def record_following() -> None:
        nonlocal following_ran
        following_ran = True

    monkeypatch.setattr(failing, "_async_prepare_connection", fail_preparation)

    with pytest.raises(GoveeBleClientError, match="preparation failed"):
        await failing.async_set_power(True)
    await asyncio.wait_for(arbiter.async_run(following, record_following), timeout=1)

    assert following_ran


@pytest.mark.asyncio
async def test_connection_arbiter_limits_priority_burst_to_protect_polling() -> None:
    """Continuous controls cannot starve a queued routine poll."""

    arbiter = GoveeConnectionArbiter()
    clients = [
        GoveeBleClient(None, f"AA:BB:CC:DD:EE:{index:02X}")
        for index in range(6)
    ]
    operation_order: list[str] = []
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def hold_lease() -> None:
        blocker_started.set()
        await release_blocker.wait()

    async def record(label: str) -> None:
        operation_order.append(label)

    blocker_task = asyncio.create_task(arbiter.async_run(clients[0], hold_lease))
    await blocker_started.wait()
    poll_task = asyncio.create_task(
        arbiter.async_run(
            clients[1],
            lambda: record("poll"),
            priority=client_module._ConnectionLeasePriority.POLL,
        )
    )
    await asyncio.sleep(0)
    command_tasks = []
    for index, client in enumerate(clients[2:]):
        command_tasks.append(
            asyncio.create_task(
                arbiter.async_run(client, lambda index=index: record(f"command-{index}"))
            )
        )
        await asyncio.sleep(0)

    release_blocker.set()
    await asyncio.wait_for(
        asyncio.gather(blocker_task, poll_task, *command_tasks), timeout=1
    )

    assert operation_order == ["command-0", "command-1", "command-2", "poll", "command-3"]


@pytest.mark.asyncio
async def test_connection_arbiter_removes_cancelled_priority_waiter() -> None:
    """Cancelling a queued control cannot strand the lease scheduler."""

    arbiter = GoveeConnectionArbiter()
    clients = [
        GoveeBleClient(None, f"AA:BB:CC:DD:EE:{index:02X}")
        for index in range(3)
    ]
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()
    poll_ran = asyncio.Event()

    async def hold_lease() -> None:
        blocker_started.set()
        await release_blocker.wait()

    async def record_poll() -> None:
        poll_ran.set()

    async def no_op() -> None:
        return None

    blocker_task = asyncio.create_task(arbiter.async_run(clients[0], hold_lease))
    await blocker_started.wait()
    command_task = asyncio.create_task(arbiter.async_run(clients[1], no_op))
    await asyncio.sleep(0)
    poll_task = asyncio.create_task(
        arbiter.async_run(
            clients[2],
            record_poll,
            priority=client_module._ConnectionLeasePriority.POLL,
        )
    )
    await asyncio.sleep(0)

    command_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await command_task
    assert not arbiter._command_waiters

    release_blocker.set()
    await asyncio.wait_for(asyncio.gather(blocker_task, poll_task), timeout=1)
    assert poll_ran.is_set()


@pytest.mark.asyncio
async def test_connection_arbiter_queue_does_not_consume_connection_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued fourth purifier still gets a full connection attempt budget."""

    from custom_components.govee_ble_air_purifier.bluetooth import transport

    arbiter = GoveeConnectionArbiter()
    first = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:01", connection_arbiter=arbiter
    )
    second = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:02", connection_arbiter=arbiter
    )
    established: list[FakeBleakClient] = []

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        connected = FakeBleakClient()
        established.append(connected)
        return connected

    async def async_disconnect(passed_client: FakeBleakClient, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(transport, "CONNECTION_TIMEOUT", 0.01)

    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_lease(_client: Any) -> None:
        first_started.set()
        await release_first.wait()

    async def no_op(_client: Any) -> None:
        return None

    first_task = asyncio.create_task(first._async_with_connection(hold_lease))
    await first_started.wait()
    second_task = asyncio.create_task(second._async_with_connection(no_op))

    await asyncio.sleep(0.02)
    assert not second_task.done()
    release_first.set()
    await asyncio.wait_for(first_task, timeout=1)
    assert await asyncio.wait_for(second_task, timeout=1) is None
    assert len(established) == 2

    await asyncio.gather(first.async_close(), second.async_close())


@pytest.mark.asyncio
async def test_connection_arbiter_bounds_a_stalled_lease_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled peer cannot leave another config entry initializing forever."""

    arbiter = GoveeConnectionArbiter()
    first = GoveeBleClient(None, "AA:BB:CC:DD:EE:01")
    second = GoveeBleClient(None, "AA:BB:CC:DD:EE:02")
    started = asyncio.Event()
    release_first = asyncio.Event()

    async def hold_lease() -> None:
        started.set()
        await release_first.wait()

    async def no_op() -> None:
        return None

    monkeypatch.setattr(client_module, "CONNECTION_LEASE_TIMEOUT", 0.01)
    first_task = asyncio.create_task(arbiter.async_run(first, hold_lease))
    await started.wait()
    with pytest.raises(
        GoveeBleClientError,
        match="Timed out waiting for another purifier's Bluetooth connection",
    ):
        await arbiter.async_run(second, no_op)

    release_first.set()
    await first_task


@pytest.mark.asyncio
async def test_connection_arbiter_never_waits_for_lease_while_holding_client_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A previous lease owner can start another poll while it is being evicted."""

    from custom_components.govee_ble_air_purifier.bluetooth import transport

    arbiter = GoveeConnectionArbiter()
    first = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:01", connection_arbiter=arbiter
    )
    second = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:02", connection_arbiter=arbiter
    )
    clients_by_address: dict[str, list[FakeBleakClient]] = {}

    async def async_establish_connection(
        _hass: Any,
        address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        connected = FakeBleakClient()
        clients_by_address.setdefault(address, []).append(connected)
        return connected

    async def async_disconnect(passed_client: FakeBleakClient, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(client_module, "DISCONNECT_TIMEOUT", 0.01)

    assert (await first.async_get_state()).is_on is True
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    original_release = first._async_release_for_connection_switch

    async def pause_release(deadline: float) -> None:
        release_started.set()
        await allow_release.wait()
        await original_release(deadline)

    monkeypatch.setattr(first, "_async_release_for_connection_switch", pause_release)

    async def no_op(_client: Any) -> None:
        return None

    second_task = asyncio.create_task(second._async_with_connection(no_op))
    await release_started.wait()
    first_task = asyncio.create_task(first.async_get_state())
    await asyncio.sleep(0)
    allow_release.set()

    assert await asyncio.wait_for(second_task, timeout=1) is None
    assert (await asyncio.wait_for(first_task, timeout=1)).is_on is True
    assert len(clients_by_address["AA:BB:CC:DD:EE:01"]) == 2

    await asyncio.gather(first.async_close(), second.async_close())


@pytest.mark.asyncio
async def test_connection_delegate_creates_default_deadline_when_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    deadlines: list[float] = []

    fake = FakeBleakClient()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        deadlines.append(deadline)
        return fake

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    monkeypatch.setattr(transport, "CONNECTION_TIMEOUT", 7.0)
    loop = asyncio.get_running_loop()
    before = loop.time()

    async def operation(_client: Any) -> None:
        return None

    await client._async_with_connection(operation)

    after = loop.time()
    assert len(deadlines) == 1
    assert before + 7.0 <= deadlines[0] <= after + 7.0

    await client.async_close()


@pytest.mark.asyncio
async def test_disconnected_callback_reconnects_and_ignores_stale_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    callbacks: list[Any] = []
    establish_count = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        callbacks.append(disconnected_callback)
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    async def operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client._async_with_connection(operation) is connected_clients[0]
    connected_clients[0].is_connected = False
    callbacks[0](connected_clients[0])
    assert await client._async_with_connection(operation) is connected_clients[1]

    callbacks[0](connected_clients[0])
    assert await client._async_with_connection(operation) is connected_clients[1]
    assert establish_count == 2

    await client.async_close()


def test_unexpected_disconnect_clears_session_and_advertisement_history(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    hass = object()
    client = GoveeBleClient(hass, "AA:BB:CC:DD:EE:FF")
    connected = FakeBleakClient()
    client._client = connected
    disconnect_signal = asyncio.Event()
    client._disconnect_signal = disconnect_signal
    client._session_key = b"session"
    client._connected_at = time.monotonic() - 120
    client._session_started_at = time.monotonic() - 110
    cleared: list[tuple[Any, str]] = []
    caplog.set_level(
        logging.DEBUG,
        logger="custom_components.govee_ble_air_purifier.bluetooth.client",
    )
    monkeypatch.setattr(
        transport,
        "clear_advertisement_history",
        lambda passed_hass, address: cleared.append((passed_hass, address)),
    )

    client._handle_disconnect(connected)

    assert client._client is None
    assert client._disconnect_signal is None
    assert disconnect_signal.is_set()
    assert client._session_key is None
    assert client._connected_at is None
    assert client._session_started_at is None
    assert client._fresh_advertisement_after is not None
    assert client._unexpected_disconnect_revision == 1
    assert cleared == [(hass, "AA:BB:CC:DD:EE:FF")]
    assert "BLE connection disconnected after 120." in caplog.text
    assert "encrypted session age: 110." in caplog.text


@pytest.mark.asyncio
async def test_failed_advertisement_recovery_is_backed_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(object(), "AA:BB:CC:DD:EE:FF")
    wait_flags: list[bool] = []

    async def async_prepare_connection_path(
        _hass: Any,
        _address: str,
        *,
        after: float | None,
        wait_for_advertisement: bool = True,
    ) -> None:
        wait_flags.append(wait_for_advertisement)
        raise GoveeBleClientError("not found")

    monkeypatch.setattr(
        transport, "async_prepare_connection_path", async_prepare_connection_path
    )

    with pytest.raises(GoveeBleClientError, match="not found"):
        await client._async_prepare_connection()
    with pytest.raises(GoveeBleClientError, match="not found"):
        await client._async_prepare_connection()

    client._advertisement_retry_at = 0.0
    with pytest.raises(GoveeBleClientError, match="not found"):
        await client._async_prepare_connection()

    assert wait_flags == [True, False, True]
    assert client._advertisement_failure_count == 2
    assert client._advertisement_retry_at > asyncio.get_running_loop().time() + 119


@pytest.mark.asyncio
async def test_operation_failure_invalidates_connection_without_replaying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    establish_count = 0
    disconnects: list[Any] = []

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        disconnects.append(passed_client)
        await passed_client.disconnect()

    attempts = 0

    async def failing_operation(_client: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("operation failed")

    async def successful_operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    with pytest.raises(ValueError, match="operation failed"):
        await client._async_with_connection(failing_operation)

    assert attempts == 1
    assert disconnects == [connected_clients[0]]
    assert (
        await client._async_with_connection(successful_operation)
        is connected_clients[1]
    )

    await client.async_close()


@pytest.mark.asyncio
async def test_idle_timeout_disconnects_and_next_operation_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import (
        client as client_module,
    )
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    monkeypatch.setattr(client_module, "CONNECTION_IDLE_GRACE", 0.0)
    client = GoveeBleClient(
        None,
        "AA:BB:CC:DD:EE:FF",
        profile=get_profile("h7126"),
        polling_interval_seconds=60,
    )
    connected_clients = [FakeBleakClient(), FakeBleakClient()]
    establish_count = 0
    disconnected = asyncio.Event()

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()
        disconnected.set()

    async def operation(passed_client: Any) -> Any:
        return passed_client

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client._async_with_connection(operation) is connected_clients[0]
    await asyncio.wait_for(disconnected.wait(), 0.1)
    assert connected_clients[0].is_connected is False

    assert await client._async_with_connection(operation) is connected_clients[1]
    assert establish_count == 2

    await client.async_close()


@pytest.mark.asyncio
async def test_notification_cleanup_failure_preserves_result_and_reconnects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(
        None, "AA:BB:CC:DD:EE:FF", profile=get_profile("h7126")
    )
    connected_clients = [
        FakeBleakClient(fail_stop_notify=True),
        FakeBleakClient(),
    ]
    establish_count = 0

    async def async_establish_connection(
        _hass: Any,
        _address: str,
        _disconnected_callback: Any,
        *,
        deadline: float,
    ) -> FakeBleakClient:
        nonlocal establish_count
        connected = connected_clients[establish_count]
        establish_count += 1
        return connected

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(
        transport, "async_establish_connection", async_establish_connection
    )
    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)

    assert await client.async_set_power(True) is True
    assert connected_clients[0].disconnected is True
    assert await client.async_set_power(True) is True
    assert establish_count == 2

    await client.async_close()


@pytest.mark.asyncio
async def test_close_waits_for_active_transaction_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from custom_components.govee_ble_air_purifier.bluetooth import transport

    client = GoveeBleClient(None, "AA:BB:CC:DD:EE:FF")
    connected = FakeBleakClient()

    async def async_disconnect(passed_client: Any, *, deadline: float) -> None:
        await passed_client.disconnect()

    monkeypatch.setattr(transport, "async_disconnect", async_disconnect)
    client._client = connected
    await client._lock.acquire()

    close_task = asyncio.create_task(client.async_close())
    await asyncio.sleep(0)

    assert close_task.done() is False
    assert connected.disconnected is False

    client._lock.release()
    await close_task

    assert connected.disconnected is True
