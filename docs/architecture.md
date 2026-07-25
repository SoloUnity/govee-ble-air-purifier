# Integration Architecture

This document describes the runtime boundaries of the Home Assistant custom
integration. For a file-by-file map, see
[`repository-structure.md`](repository-structure.md).

## Runtime Branches

The integration is not a linear stack. Home Assistant setup, polling, direct
commands, and Custom Auto share runtime objects but follow distinct branches:

```text
Home Assistant config flow ---> setup_helpers.py + profiles.py

Home Assistant config entry ---> __init__.py ---> GoveeRuntimeData
                                                    |       |       \
                                                    |       |        diagnostics
                                                    |       |
                         fan/sensor/switch entities |       CustomAutoController
                                            \       |       /
                                             GoveeCoordinator
                                                    |
                                              GoveeBleClient
                                              /       |      \
                                      ModelProfile  protocol  transport
                                           |          |          |
                                           +----> H7124 logic    |
                                                      |          |
                                             generic framing     |
                                                                 |
Home Assistant Bluetooth <---------------------------------------+
             |
          purifier
```

The branches have these boundaries:

- Root Home Assistant entrypoints are `__init__.py`, `config_flow.py`,
  `diagnostics.py`, and the active `fan.py`, `sensor.py`, and `switch.py`
  platform modules.
- `__init__.py` is the composition root for a loaded config entry. It creates
  one client, coordinator, and Custom Auto controller, stores them in
  `ConfigEntry.runtime_data`, forwards platform setup, reloads on option
  changes, and stops runtime work after successful platform unload.
- Entities consume the shared runtime objects; they do not create BLE clients
  or controllers.
- The retired `select` platform is not loaded. Fan mode is exposed by the fan
  entity, while the switch represents integration-managed Custom Auto
  ownership.

## Configuration

`config_flow.py` implements user setup and options. It uses
`setup_helpers.py` for cached Bluetooth advertisement selection, address
normalization, and polling interval handling, and `profiles.py` to match a
supported model. Manual address entry still requires compatible advertisement
evidence already available from Home Assistant.

Config entry data identifies the address, name, and profile. Options contain
the polling interval and Custom Auto thresholds and delays. Saving options
reloads the entry so the coordinator interval and controller configuration are
rebuilt consistently.

`custom_auto/config.py` owns the Custom Auto defaults, bounded integer parsing,
ordering validation, hysteresis validation, and conversion between config-entry
options and immutable `CustomAutoConfig`. Setup, options, and runtime loading
use this same boundary.

## Models And Protocol

`models.py` defines two deliberately different immutable values:

- `DecodedStatus` is the narrow result of decoding one H7124 `aa19` status
  frame. It contains PM2.5 and filter life only.
- `PurifierState` is the application-facing snapshot exchanged by the BLE
  client, coordinator, entities, diagnostics, and Custom Auto. It also carries
  power and the integration's known fan mode.

`bluetooth/framing.py` is generic frame infrastructure. It builds 20-byte Govee
frames and validates frame length and XOR checksum. It does not know H7124
commands, response markers, or field offsets.

Root `protocol.py` is H7124-specific. It defines power, state-query, status-query,
and fan-mode commands; identifies H7124 response and push frames; confirms
commands; and decodes power, mode pushes, and `DecodedStatus`. Its decoders call
the generic frame validator before interpreting bytes. A structurally valid
status frame with PM2.5 above the supported range decodes to `None` for that
measurement.

`profiles.py` packages the H7124 UUIDs, commands, matchers, decoders, advertised
name prefixes, and capabilities as `ModelProfile`. Higher layers receive a
profile rather than duplicating those constants.

## Bluetooth Ownership

`bluetooth/client.py` owns one serialized transaction lock per configured
purifier. A transaction deadline starts before lock acquisition and is shared
by lock waiting, connection establishment when needed, notification setup,
writes, response waits, notification cleanup, and failure cleanup.

The client caches a healthy GATT connection across transactions. Every
successful operation resets a 30-second idle timer, so the default 10-second
polling interval normally retains one connection while intervals above 30
seconds release it between polls. Idle cleanup acquires the transaction lock,
and entry shutdown cancels pending idle cleanup before closing the connection.
An unexpected-disconnect callback clears only the client instance that raised
it; identity checking prevents a delayed callback from clearing a replacement.
The next poll or command reconnects through Home Assistant. Transaction failure
invalidates the connection but does not replay the operation.

For polling, `GoveeBleClient.async_get_state()` uses one transaction-scoped
notification subscription to issue the power and status queries in sequence.
The underlying connection may have been retained from an earlier transaction.
The notification handler accepts only the matcher for the current request,
validates the frame, and resolves its pending future. Commands use the same
serialized path and publish no success merely because a write completed; they
wait for a matching confirmation. Power-on plus mode can be sent in one locked
transaction. Notification cleanup failure preserves an otherwise successful
result but discards the connection before another operation can use it.

`bluetooth/client.py` owns transaction serialization, writes, notification
subscription, response matching, notification cleanup, connection reuse, idle
release, and disconnect-callback state. It delegates Home Assistant connection
mechanics to `bluetooth/transport.py`.

`bluetooth/transport.py` owns Home Assistant's connectable BLE-device lookup,
stale-connection cleanup before establishment, connection establishment, and
bounded best-effort disconnect. It applies the caller's existing deadline
without extending it and suppresses disconnect cleanup errors.

## Coordinator Publication

`GoveeCoordinator` is the shared `DataUpdateCoordinator` and the command-side
publication boundary. Its `_state_lock` prevents polls and commands from
publishing concurrently.

A successful poll merges the client's `PurifierState` into coordinator data:

- Power and filter life come from the latest poll.
- A valid PM2.5 sample replaces the cached value and increments
  `pm25_sample_revision`, even when the number is unchanged.
- An invalid PM2.5 sample retains the prior display value but marks
  `last_pm25_update_success` false, so cached data cannot drive Custom Auto.
- The last integration-commanded fan mode is retained because the verified poll
  responses do not report all manual modes.

Poll failures become Home Assistant `UpdateFailed` failures. Entity availability
follows coordinator health rather than the presence of cached values.

After a confirmed command, the coordinator calls `async_set_updated_data()`
with a new `PurifierState` immediately, then schedules a delayed refresh to
reconcile with the purifier. Command-side publication does not increment the
PM2.5 sample revision, so it cannot masquerade as a fresh air-quality reading.
If known power is off, setting a mode uses the client's combined power-and-mode
transaction where available.

## Custom Auto Control Flow

`custom_auto/policy.py` is pure policy. It defines the five percentages and
mode mappings, the two-sample upshift requirement, and `speed_for_pm()`. Upward
thresholds use strict `>` comparisons.

`custom_auto/controller.py` owns mutable behavior: activation and restored
speed, coordinator subscription, queued sample revisions, upshift confirmation,
downshift timer tasks, mature targets, command retry gating, ownership handoff,
and controller-state listeners. It never communicates with BLE directly.

The publication path is:

```text
BLE poll
  -> coordinator publishes PurifierState and a fresh PM2.5 revision
  -> controller listener captures the sample and schedules evaluation
  -> pure policy determines any required upshift
  -> controller updates confirmations and downshift timers
  -> controller requests a coordinator fan-mode command when action is due
  -> coordinator waits for BLE confirmation and publishes command state
  -> command publication has no new PM2.5 revision and is not counted as a sample
```

An upshift requires two distinct valid revisions that both require a speed above
the current speed; the second reading determines the target. A reading that does
not require an increase clears pending upward confirmation.

Each lower speed has an independent timer. A valid reading at or below its
downward threshold qualifies because the controller checks `pm25 <= threshold`.
Only a valid reading above that threshold cancels a pending timer or clears a
mature target. Invalid samples and poll failures preserve elapsed time but
cannot trigger a downshift command.

After an automatic command failure, the controller waits for a later successful
PM2.5 coordinator update before retrying. User commands that transfer ownership
run through `async_handoff()`: the controller retains ownership until the
coordinator command succeeds and best-effort reasserts the prior speed on
failure.

The switch restores Custom Auto ownership and controlled speed. The fan exposes
manual percentages and the purifier's hardware Auto preset; while Custom Auto
owns control, the fan reports logical Auto while the controller sends manual
speed commands.

## Concurrency

When all three locks are involved, acquisition proceeds in this order:

```text
CustomAutoController._lock
  -> GoveeCoordinator._state_lock
     -> GoveeBleClient._lock
```

Coordinator callbacks only schedule controller evaluation; they do not await
the controller lock. This avoids lock inversion. The locks separately protect
policy ownership, shared-state publication, and BLE request/notification state.

## Runtime Setup And Cleanup

Config entry setup resolves the profile, creates the client and coordinator,
performs the first refresh, creates `CustomAutoController` from
`CustomAutoConfig`, stores `GoveeRuntimeData`, and forwards the active fan,
sensor, and switch platforms. Any setup failure stops the controller if it was
created and shuts down the coordinator so a successful first refresh cannot
leak its retained connection.

After successful platform unload, the controller removes listeners and cancels
evaluation and timer tasks, then the coordinator cancels its delayed refresh
and shuts down polling before closing the BLE client.

## Test Lanes

The fast behavioral lane runs without installing Home Assistant. `tests/conftest.py`
provides only the coordinator-shaped Home Assistant stub needed at collection
time, while focused tests supply their own entity and flow substitutes. CI runs
this lane with:

```bash
python -m pytest --ignore=tests/test_runtime_smoke.py
```

The real-Home-Assistant smoke lane installs supported Home Assistant versions
and runs only `tests/test_runtime_smoke.py`. It verifies runtime imports,
inheritance and signatures, platform entity construction, config flow behavior,
and setup/unload composition against actual Home Assistant APIs without using a
physical purifier.

The main test boundaries are:

- `tests/bluetooth/test_framing.py`, `test_client.py`, and `test_transport.py`
  cover generic framing, serialized notification transactions, deadlines,
  connection reuse, idle release, reconnection, and Home Assistant connection
  ownership.
- `tests/custom_auto/test_config.py`, `test_policy.py`, and `test_controller.py`
  cover parsing and validation, pure speed selection, and mutable timer and
  ownership behavior.
- Root `tests/test_protocol.py`, coordinator, entity, config-flow, diagnostics,
  lifecycle, persistence, packaging, and runtime-smoke files cover integration
  boundaries.

Physical BLE interoperability remains a device-level verification concern.

## Maintainer Invariants

1. One client, coordinator, and controller exist per loaded config entry.
2. BLE transactions for a purifier do not overlap.
3. Polls and commands do not publish coordinator state concurrently.
4. Commands publish state only after purifier confirmation.
5. Invalid or command-side cached PM2.5 data does not drive Custom Auto.
6. Downshift equality qualifies; only a valid reading above the boundary resets
   its timer or mature target.
7. Model-specific byte interpretation remains below the coordinator.
8. Unload leaves no controller timers, delayed coordinator refresh, idle
   disconnect task, or retained BLE connection.
