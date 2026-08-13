# Integration Architecture

This document describes the runtime boundaries of the Home Assistant custom
integration. For a file-by-file map, see
[`repository-structure.md`](repository-structure.md).

## Runtime Branches

The integration is not a linear stack. Home Assistant setup, polling, direct
commands, and Custom Auto share runtime objects but follow distinct branches:

```text
Home Assistant config flow ---> setup_helpers.py + profiles.py
                                                        |
                                                model_profiles/*.json

Home Assistant config entry ---> __init__.py ---> GoveeRuntimeData
                                                    |       |       \
                                                    |       |        diagnostics
                                                    |       |
                    fan/light/sensor/switch entities |       CustomAutoController
                                             \       |       /
                                              AutoResumeManager
                                                     |
                                              GoveeCoordinator
                                                    |
                                              GoveeBleClient
                                              /       |      \
                                      ModelProfile  protocol  transport
                                           |          |          |
                              model_profiles/*.json   |          |
                              (per-model GATT UUIDs   |          |
                               and outbound 20-byte   |          |
                               command frames)        |          |
                                           +--------> shared     |
                                                      frame      |
                                                      validation |
                                                      matching   |
                                                      decoding   |
                                                        |        |
                                             generic framing     |
                                                                  |
Home Assistant Bluetooth <---------------------------------------+
              |
           purifier
```

The branches have these boundaries:

- Root Home Assistant entrypoints are `__init__.py`, `config_flow.py`,
  `diagnostics.py`, and the active `fan.py`, `light.py`, `sensor.py`, and
  `switch.py` platform modules.
- `__init__.py` is the composition root for a loaded config entry. It creates
  one client, coordinator, Custom Auto controller, and Auto resume manager,
  stores them in
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
recognized `H712*` family model from its advertised name. Manual address entry
still requires compatible advertisement evidence already available from Home
Assistant. Setup remains a manually started flow with a recently seen BLE
picker; there is no automatic integration discovery.

Config entry data identifies the address, name, and profile. The stored
profile is the exact detected lowercase model key (for example `h7126`), even
when `default.json` supplies its behavior, so a later exact model JSON takes
effect after an update and restart. Existing entries without a stored profile
and existing `h7124` entries resolve to H7124, and the BLE address remains the
unique ID. Setup resolves the profile before displaying polling, allowing the
form to use the profile-defined default (10 seconds for H7124/fallback and 3
seconds for H7129). Options contain the polling interval and, when the profile
supports the required modes, Custom Auto thresholds and delays. Saving options
reloads the entry so the coordinator interval and controller configuration are
rebuilt consistently.

`custom_auto/config.py` combines profile-defined PM2.5 boundaries with shared
confirmation and downshift-delay defaults. It owns bounded integer parsing,
ordering validation, and conversion between config-entry options and immutable
`CustomAutoConfig`. Setup, options, and runtime loading use this same boundary.

## Models And Protocol

`models.py` defines three deliberately different immutable values:

- `DecodedStatus` is the narrow result of decoding one status frame (the
  tested H7124 definition decodes an `aa19` frame). It contains PM2.5 and
  filter life only.
- `PurifierState` is the application-facing snapshot exchanged by the BLE
  client, coordinator, entities, diagnostics, and Custom Auto. It also carries
  power, the integration's known fan mode, and optional `NightLightState`.
- `NightLightState` independently carries known light power, device brightness
  percentage, and queried or command-confirmed RGB color.

`bluetooth/framing.py` is generic frame infrastructure. It builds 20-byte Govee
frames and validates frame length and XOR checksum. It does not know model
commands, response markers, or field offsets.

Root `protocol.py` is shared across the recognized `H712*` family. It retains
shared frame validation, response matching, command confirmation, and status
decoding. Its decoders call the generic frame validator before interpreting
bytes. A structurally valid status frame with PM2.5 above the supported range
decodes to `None` for that measurement. Models whose response semantics or
framing differ from the tested H7124 behavior still require Python changes
here; they cannot be added by JSON alone.

`model_profiles/` holds one complete JSON definition per model. `default.json`
and `h7124.json` contain the physically tested H7124 definition; `h7129.json`
contains the capture-derived H7129 definition. Each file owns its GATT service
and characteristic UUIDs, transport encryption selection, exact outbound
20-byte power, query, and fan-mode command frames, and optional Custom Auto
PM2.5 boundaries. Exact profiles may also own an optional `night_light`
capability with static calls and variable brightness/RGB templates. The JSON
does not own encryption mechanics or response interpretation. Future model
files are complete definitions, not partial inheritance over another file.

`profiles.py` searches advertised names case-insensitively for `H712` plus one
ASCII letter or digit (for example `GVH7124`, `GVH712C`, or
`ihoment_H7129_6A7D`), loads the exact lowercase model JSON when present (for
example `h7126.json`), and otherwise falls back to `default.json`, which means
H7124 protocol behavior. It packages the resolved UUIDs, commands, matchers,
decoders, advertised identity, and capabilities as `ModelProfile`. Higher
layers receive a profile rather than duplicating those constants. The H7124
integration is physically tested. Physical H7129 integration evidence confirms
connection, encrypted handshake, polling, disconnect, and recovery. Its
state-changing commands are implemented from decrypted captures but have not
yet been physically validated through this integration. Fallback models are
unverified and may fail or expose unsupported or mismatched features.

Fan-mode lists may vary by exact profile. The integration creates the Custom
Auto switch only when the profile provides Sleep, Low, Medium, High, Turbo,
and hardware Auto, preventing the H7124-specific policy from requesting a mode
that a narrower profile does not define.

Night-light availability is separately gated only by the optional profile
block. H7124 and H7129 define it; `default.json` does not, so unverified fallback
models do not expose the light. Both exact profiles use the same plaintext
commands, with H7129 encryption remaining a transport concern.

## Bluetooth Ownership

`bluetooth/govee_v1.py` owns only the H7129 frame transform and handshake frame
semantics. It applies AES-128-ECB to bytes 0-15, applies the captured
RC4-compatible transform to bytes 16-19, builds `e7 01` and `e7 02` requests,
and validates their plaintext responses. Application frames remain the same
checksum-valid plaintext values used by the H7124 path.

`bluetooth/client.py` owns one serialized transaction lock per configured
purifier. Before an uncached connection, it asks the transport to verify a
connectable path. A post-disconnect connection requires an advertisement newer
than the disconnect. An already-running idle release gets its own 5-second
preflight cleanup wait, and fresh-advertisement recovery gets up to 10 seconds.
Neither consumes the subsequent lock or application budget. Connection and
service discovery then have a separate 25-second deadline. A newly connected
H7129 has a separate 10-second handshake deadline. Transaction-lock waiting and
the mandatory application exchange use the operation's normal budget: 5 seconds
for the two-response power/status poll and 2 seconds for command confirmation.
Profiles with a night light then get a separate 1-second best-effort telemetry
budget. The application budget starts after connection and handshake complete.
Explicit disconnect
cleanup has its own 5-second bound. Timeout errors identify idle cleanup, lock
waiting, a write/setup stage, or an actual purifier response rather than
claiming a response timeout before a request was sent.

The client caches a healthy GATT connection across transactions. Every
successful operation resets an idle timer derived from the configured polling
interval. Intervals from 5 through 25 seconds use the interval plus a 5-second
margin, retaining the connection through the next expected poll without
exceeding 30 seconds. Longer intervals use a 5-second grace for command bursts
and the coordinator's delayed refresh, then release the connection rather than
occupying a Bluetooth slot without reaching the next poll. Idle cleanup
acquires the transaction lock, and entry shutdown cancels pending idle cleanup
before closing the connection. An unexpected-disconnect callback clears only
the client instance that raised it; identity checking prevents a delayed
callback from clearing a replacement. The next poll or command reconnects
through Home Assistant. Transaction failure invalidates the connection but does
not replay the operation. A confirmed link loss may replay one read-only state
poll after fresh-advertisement recovery; commands are never replayed. A
connection-scoped disconnect signal wakes handshake and application waits for
that exact client, while delayed callbacks from an older client cannot affect a
replacement.

For a profile selecting Govee V1 encryption, a newly established connection
completes the `e7 01` / `e7 02` exchange before its first application operation.
The client encrypts only at the GATT write boundary and decrypts before the
shared matcher and decoder path. If session-key decryption fails during an
application transaction, a checksum-valid communication-key `e7 01` or `e7 02`
notification is stale handshake traffic and is ignored without resolving the
pending response. All other failures retain the original error path. A healthy
cached connection reuses its session key; disconnect, replacement, failure, idle
release, and close all discard it. Every reconnect negotiates a new key.
Plaintext profiles bypass these transforms. Connection, handshake, and
application work use separate bounded phases: a slow BlueZ connection cannot
consume the shorter response timeout, and application timing begins only after
any encrypted handshake succeeds. Ignored handshake traffic does not reset that
deadline, so stale-only traffic still times out normally. Lifecycle logs report
connection and session ages under a stable short hashed device label, but never
the full Bluetooth address, packet payloads, or key material. Per-request debug
summaries report write duration, matching-response latency, and counts of total,
ignored-handshake, and nonmatching notifications. Response timeouts report
the same counts so missing callbacks can be distinguished from unexpected
traffic without exposing frame contents.

For polling, `GoveeBleClient.async_get_state()` uses one transaction-scoped
notification subscription to issue the mandatory purifier power and status
queries in sequence. Profiles with a night light then write the power/brightness
and RGB state queries back-to-back. Their responses are matched independently,
may arrive in either order, and are collected within a short shared grace
period. Missing, partial, malformed, or late optional telemetry does not discard
the core purifier state or invalidate an otherwise healthy connection.
The underlying connection may have been retained from an earlier transaction.
The notification handler ignores identified stale handshake traffic and
validates every candidate frame. During the mandatory phase it accepts only the
current request's matcher; during optional collection it checks every unresolved
light matcher. Ignored traffic neither consumes a response slot nor resends a
request. Commands use the same serialized path and publish no success merely
because a write completed; they wait for a matching confirmation. Power-on plus
mode can be sent in one locked transaction. Notification cleanup failure
preserves an otherwise successful result but discards the connection before
another operation can use it.

`bluetooth/client.py` owns transaction serialization, writes, notification
subscription, response matching, notification cleanup, connection reuse, idle
release, and disconnect-callback state. It delegates Home Assistant connection
mechanics to `bluetooth/transport.py`.

`bluetooth/transport.py` owns Home Assistant's connectable BLE-device lookup,
fresh-advertisement and per-scanner path preparation, stale-connection cleanup
before establishment, connection establishment, and bounded best-effort
disconnect. On supported Home Assistant versions it clears static advertisement
deduplication after a GATT session. Advertisement recovery uses Active callback
semantics. Home Assistant 2026.6 and newer can best-effort request a temporary
Automatic-to-Active window, subject to upstream duration clamping; the request
is made only while waiting for a new advertisement, never for an accepted
cached path. Older versions may require an explicitly Active scanner when
active discovery is needed. Failed waits back off from 60 to 300 seconds. Transport
connection stages share a dedicated deadline and bounded upstream retry count;
disconnect cleanup errors are suppressed.

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
- Night-light power and brightness come from the latest light report. A decoded
  RGB report replaces cached color; an unknown discriminator such as H7129
  `0xfc` preserves a command-confirmed runtime color instead of inventing or
  erasing one.

Poll failures become Home Assistant `UpdateFailed` failures. Entity availability
follows coordinator health rather than the presence of cached values.

After a confirmed command, the coordinator calls `async_set_updated_data()`
with a new `PurifierState` immediately, then schedules a delayed refresh to
reconcile with the purifier. Command-side publication does not increment the
PM2.5 sample revision, so it cannot masquerade as a fresh air-quality reading.
If known power is off, setting a mode uses the client's combined power-and-mode
transaction where available.

Night-light services use the same coordinator lock. A known-off or unknown
light is powered on before requested settings; a known-on light receives only
the requested brightness or RGB writes. Each confirmed step is published, so a
later failure raises to Home Assistant without discarding earlier confirmed
state. The light does not restore RGB after restart.

## Custom Auto Control Flow

`custom_auto/policy.py` is pure policy. It defines the five percentages and
mode mappings, the maximum two-sample upshift requirement, and `speed_for_pm()`.
Each model's four shared boundaries use strict `>` comparisons for upshifts and
inclusive `<=` comparisons for delayed downshifts.

`custom_auto/controller.py` owns mutable behavior: activation and restored
speed, coordinator subscription, queued sample revisions, one-shot upshift
confirmation polls, downshift timer tasks, mature targets, command retry gating,
ownership handoff, and controller-state listeners. It never communicates with
BLE directly.

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

With a positive confirmation delay, an upshift requires two distinct valid
revisions that both require a speed above the current speed; the second reading
determines the target. A reading that does not require an increase clears
pending upward confirmation. Initial detection therefore remains bounded by the
configured polling interval. After the first upward reading, the controller
schedules one full-state confirmation poll after the configured delay. A newer
valid reading can confirm sooner and makes that dedicated attempt unnecessary.
If the dedicated poll fails or returns invalid PM2.5, the first reading remains
pending and confirmation waits for the next normal poll; the dedicated attempt
is not retried. A zero confirmation delay bypasses this state and upshifts from
the first valid reading.

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

`auto_resume.py` owns integration-known automatic-mode intent. It serializes
explicit fan and switch commands, suspends hardware Auto or Custom Auto after a
confirmed power-off, and resumes that mode after either a Home Assistant
turn-on or a fresh poll detects physical power-on. A poll revision distinguishes
fresh device observations from command-side coordinator publications, so a
resume command cannot trigger itself recursively.

The manager restores the newest replicated fan or Custom Auto entity record
before platform setup, including migration of the former switch-only Custom
Auto record. This keeps persistence working if either entity is disabled and
prevents a stale disabled-entity record from winning. Restoring suspended intent
never powers on an off purifier. The switch reports Custom Auto selected while
active or suspended, and its attributes distinguish actual controller activity
from the remembered selection. While Custom Auto owns control, the fan reports
logical Auto while the controller sends manual speed commands.

## Concurrency

When all four locks are involved, acquisition proceeds in this order:

```text
AutoResumeManager._lock
  -> CustomAutoController._lock
     -> GoveeCoordinator._state_lock
        -> GoveeBleClient._lock
```

Coordinator callbacks only schedule controller or Auto-resume evaluation; they
do not await either runtime lock. This avoids lock inversion. The locks
separately protect remembered intent, policy ownership, shared-state
publication, and BLE request/notification state.

## Runtime Setup And Cleanup

Config entry setup resolves the profile, creates the client and coordinator,
performs the first refresh, creates `CustomAutoController` from
`CustomAutoConfig`, creates `AutoResumeManager`, stores `GoveeRuntimeData`, and
forwards the active fan, light, sensor, and switch platforms. The light platform
adds no entity when the profile capability is absent. Any setup failure stops
the manager and controller if they were created and shuts down the coordinator
so a successful first refresh cannot leak its retained connection.

After successful platform unload, the Auto resume manager removes its listener
and cancels reconciliation, the controller removes listeners and cancels
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
physical purifier. The matrix includes 2026.5 and 2026.6 independently to cover
the separate advertisement-history and on-demand Active-scan API boundaries.

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

1. One client, coordinator, Custom Auto controller, and Auto resume manager
   exist per loaded config entry.
2. BLE transactions for a purifier do not overlap.
3. Polls and commands do not publish coordinator state concurrently.
4. Commands publish state only after purifier confirmation.
5. Invalid or command-side cached PM2.5 data does not drive Custom Auto.
6. Downshift equality qualifies; only a valid reading above the boundary resets
   its timer or mature target.
7. Model-specific byte interpretation remains below the coordinator. JSON
   model definitions own only GATT UUIDs and outbound command frames; response
   matching, confirmation, and decoding semantics stay in Python.
8. Unload leaves no Auto-resume task, controller timer, delayed coordinator
   refresh, idle disconnect task, or retained BLE connection.
