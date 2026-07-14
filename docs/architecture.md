# Integration Architecture and Runtime Interactions

This document explains how the Govee BLE Air Purifier integration is assembled,
how data and commands move through it, and which components own synchronization,
state, recovery, and Home Assistant presentation.

It is intended for maintainers debugging runtime behavior or extending the
integration. For a file-oriented overview, see
[`repository-structure.md`](repository-structure.md).

## Architectural Overview

The integration is divided into six layers:

```text
Home Assistant UI and services
            |
            v
Fan / sensor / Custom Auto switch entities
            |
            +--------------------------+
            |                          |
            v                          v
   GoveeCoordinator          CustomAutoController
            |                          |
            +-------------+------------+
                          |
                          v
                  GoveeBleClient
                          |
                          v
             ModelProfile / protocol
                          |
                          v
                    BLE purifier
```

Supporting these runtime layers are:

- `config_flow.py` and `setup_helpers.py`, which create and update config
  entries.
- `__init__.py`, which constructs the runtime object graph for each config
  entry.
- `diagnostics.py`, which presents a redacted view of configuration and runtime
  state.
- `strings.json` and `translations/en.json`, which define user-facing text.

The layers have intentionally different responsibilities:

- Protocol code understands bytes but not Home Assistant.
- The BLE client understands transactions but not entities.
- The coordinator owns shared device state and command serialization.
- The Custom Auto controller owns automatic speed policy and timer state.
- Entities translate Home Assistant operations into coordinator or controller
  calls.

## Runtime Object Graph

Every loaded config entry owns one instance of each primary runtime object:

```text
ConfigEntry.runtime_data (GoveeRuntimeData)
    |
    |-- profile: ModelProfile
    |
    |-- coordinator: GoveeCoordinator
    |       `-- client: GoveeBleClient
    |
    `-- controller: CustomAutoController
            `-- coordinator: same GoveeCoordinator
```

The entity platforms receive these shared objects from `entry.runtime_data`.
They do not construct their own clients, coordinators, or controllers. This is
important because all entities for one purifier must see the same state and use
the same locks.

`GoveeRuntimeData` is defined in `coordinator.py`. It contains:

- The coordinator used by every entity.
- The selected model profile.
- The Custom Auto controller used by both the fan and switch entities.

## Persisted Configuration

The config entry divides values into `data` and `options`.

### Entry Data

Entry data identifies the device:

- `address`: canonical BLE MAC or UUID address.
- `name`: user-facing device name.
- `profile`: model profile key, currently `h7124`.

The normalized address becomes the config entry unique ID. Entity unique IDs
are derived from that entry unique ID, so address validation must occur before
duplicate-entry handling.

### Entry Options

Options control runtime policy:

- Polling interval.
- Four upward PM2.5 thresholds.
- Four downward PM2.5 thresholds.
- Four downward delay durations.

The legacy `use_custom_auto` option is removed when options are saved. Custom
Auto activation is now represented by the switch entity's restored state, not a
configuration toggle.

Changing options triggers the update listener registered by `__init__.py`. The
listener reloads the config entry, which reconstructs the coordinator and
controller with the new polling interval and Custom Auto configuration.

## Configuration Flow

`GoveeBleAirPurifierConfigFlow.async_step_user()` drives initial setup.

### Device Selection

When the form opens, the integration requests an active Bluetooth scan when the
installed Home Assistant version provides that API. It then reads connectable
advertisements from Home Assistant's Bluetooth cache.

`setup_helpers.build_discovered_device_options()` performs the following work:

1. Match each advertisement name to a registered `ModelProfile`.
2. Reject malformed platform addresses.
3. Deduplicate advertisements by normalized address.
4. Keep the strongest advertisement for each address.
5. Sort options by RSSI and format a label with signal and source information.

The user can select a discovered purifier or enter an address manually. Manual
addresses are canonicalized by `profiles.canonicalize_ble_address()` and must
have cached advertisement evidence. The advertisement name must match a known
profile before an entry can be created.

This means manual entry is an address-entry fallback, not a way to bypass model
validation.

### Unique IDs and Duplicate Entries

After validation, the canonical address is normalized into a stable unique ID.
Only then does the flow ask Home Assistant to abort or update an already
configured entry. This ordering prevents malformed text from colliding with an
existing device and overwriting its valid address.

### Custom Auto Configuration

The second setup step gathers Custom Auto thresholds and delays. The form is
organized around four boundaries between five speeds:

```text
Excellent       Good          Fair          Bad           Poor
20% Sleep  <->  40% Low  <->  60% Medium <-> 80% High <-> 100% Turbo
```

`controller.parse_custom_auto_values()` converts form values to bounded
integers. `controller.validate_custom_auto_values()` enforces:

- Strictly ascending upward thresholds.
- Strictly ascending downward thresholds.
- A downward threshold no greater than the upward threshold for the same
  boundary.

The same parsing and validation functions are used by setup, options, and
runtime configuration loading. This avoids different interpretations of the
same stored options.

## Config Entry Setup and Unload

`__init__.py.async_setup_entry()` is the composition root.

Setup occurs in this order:

1. Read the address and resolve the stored profile key.
2. Construct `GoveeBleClient` with the Home Assistant instance, address, and
   profile.
3. Construct `GoveeCoordinator` with the client and configured polling interval.
4. Run `async_config_entry_first_refresh()`.
5. Construct `CustomAutoController` from the validated options.
6. Store the coordinator, profile, and controller in `entry.runtime_data`.
7. Register the options update listener.
8. Forward setup to the fan, sensor, and switch platforms.

The first refresh happens before entity creation. A purifier that cannot be
read therefore fails config entry setup instead of creating entities with no
initial state.

Unload reverses the ownership chain:

1. Ask Home Assistant to unload all entity platforms.
2. Stop the Custom Auto controller, including listeners and background tasks.
3. Shut down the coordinator and cancel its delayed refresh.

Controller and coordinator cleanup only occurs after platform unload succeeds,
so a failed Home Assistant unload does not leave still-loaded entities pointing
at already-destroyed runtime objects.

## Protocol Layer

`protocol.py` contains the H7124 frame rules and has no Home Assistant runtime
dependencies.

### Frame Format

Commands and responses are 20 bytes long:

- Bytes 0 through 18 contain the body, padded with zeroes where necessary.
- Byte 19 contains the XOR checksum of the body.

`build_frame()` creates frames and `validate_frame()` verifies length and
checksum. Decoders call `validate_frame()` before reading fields.

### Commands and Responses

The module defines:

- Power-on and power-off commands.
- Power-state and status queries.
- Manual, Sleep, hardware Auto, and Turbo mode commands.
- Matchers for power, status, mode push, and exact command echo frames.
- Decoders for power, PM2.5, filter life, and mode push notifications.

State and status response matchers include known response-marker bytes. They do
not match solely on the command prefix. This prevents an outbound query echoed
by the device from being decoded as a valid off or zero-valued response.

PM2.5 values above the supported maximum are represented as `None`. This is a
semantic invalid sample, distinct from a malformed frame, checksum failure, or
transport failure.

### Profiles

`profiles.py` packages protocol behavior into `ModelProfile`. A profile owns:

- Model identity and advertised name prefixes.
- Service, write, and notification UUIDs.
- Commands and fan-mode capabilities.
- Response matchers and decoders.

Higher layers consume profile fields rather than importing H7124 constants
directly wherever possible. Adding a model should primarily require a new
profile and any genuinely different protocol functions.

## BLE Client Transactions

`GoveeBleClient` turns profile operations into serialized request/response BLE
transactions.

### Poll Transaction

`async_get_state()` performs one connection and notification subscription for
two requests:

```text
Acquire client lock
    -> locate connectable BLEDevice through Home Assistant
    -> close stale connections
    -> establish connection
    -> subscribe to notifications
    -> write power-state query
    -> wait for matching validated response
    -> write status query
    -> wait for matching validated response
    -> stop notifications
    -> disconnect
    -> release client lock
```

The resulting frames are decoded through the active profile and converted to
`GoveeData` for the coordinator.

### Command Transactions

Power and fan-mode methods use the same transaction machinery but different
matchers:

- Power waits for a decoded power confirmation matching the requested state.
- Fan mode accepts an exact valid command echo or a matching mode push.
- Power-and-mode batches both commands in one connection when an off purifier
  must be started at a requested speed.

No command is reported as successful merely because the write completed. The
client waits for a matching notification.

### Serialization and Deadlines

The client owns `_lock`, which allows only one BLE transaction per purifier at
a time. The transaction deadline starts before lock acquisition and is shared
by every stage. Each await uses the remaining time instead of receiving a new
full timeout.

The deadline therefore bounds:

- Waiting behind another transaction.
- Stale-connection cleanup.
- Connection establishment.
- Notification setup.
- Every command write and response wait.
- Notification cleanup.
- Disconnect cleanup.

Notification and disconnect cleanup failures are logged at debug level and do
not replace an already successful result or a more useful primary exception.
Pending response futures are canceled when a transaction exits.

## Coordinator State Management

`GoveeCoordinator` is both a Home Assistant `DataUpdateCoordinator` and the
integration's device-state command boundary.

### Shared State

`GoveeData` contains the state consumed by entities and Custom Auto:

- `is_on`
- `pm25`
- `filter_life`
- `fan_mode`

This differs from `models.GoveeAirPurifierState`, which is a protocol-decoding
result. `GoveeData` is the application-facing state shared across runtime
components.

### Polling

`_async_update_data()` calls `client.async_get_state()` while holding the
coordinator `_state_lock`.

On success it:

1. Marks the overall poll successful.
2. Separately records whether PM2.5 was valid.
3. Publishes current power and filter life.
4. Publishes a new PM2.5 value when valid, otherwise retains the previous cached
   value.
5. Preserves the last integration-commanded fan mode when polling does not
   provide one.

Retaining cached PM2.5 allows diagnostics and display code to retain the last
measurement. Availability still follows the coordinator's update status, and
Custom Auto consults `last_pm25_update_success` before acting on the cached
number.

On transport or protocol failure the coordinator raises Home Assistant's
`UpdateFailed` and clears both success flags.

### Commands

`async_set_power()` and `async_set_fan_mode()` also hold `_state_lock`. This
prevents a poll and a command, or two commands, from reading and publishing
state concurrently.

After device confirmation, commands publish an updated `GoveeData` immediately.
They then schedule a refresh one second later. Immediate publication makes the
UI responsive; delayed polling reconciles the command-side state with the
physical device.

If a mode is requested while known power state is off, the coordinator uses the
client's combined power-and-mode transaction. This avoids a stale check followed
by separately serialized operations.

### Fan Mode Limitation

The current poll responses do not provide a verified fan-mode field. The
coordinator therefore tracks the last mode commanded by this integration. A
physical control or another application can change mode without that change
being observable through the current polling protocol.

## Home Assistant Entities

All active entities inherit from `GoveeAirPurifierEntity` in `entity.py`.

The base entity:

- Subscribes to the shared coordinator through `CoordinatorEntity`.
- Builds a stable unique ID from the config entry unique ID and entity key.
- Places all entities under one Home Assistant device using the integration
  domain and config entry unique ID.
- Supplies manufacturer, model, and configured device name.

### Fan Entity

`fan.py` is the main control surface.

The profile's modes are separated into:

- Manual speeds ordered as Sleep, Low, Medium, High, and Turbo.
- Hardware Auto, represented as a preset.

Home Assistant percentages are mapped to the ordered manual speed list. The fan
remembers the last manual speed so switching from manual speed to hardware Auto
and back to Manual restores the previous speed.

When Custom Auto owns the device:

- `preset_mode` remains Auto.
- `percentage` comes from `CustomAutoController.current_speed`.
- The actual purifier mode is one of the manual speed commands.

User operations that take ownership away from Custom Auto, including power off,
manual percentage, Manual preset, or hardware Auto, use
`CustomAutoController.async_handoff()` rather than deactivating directly.

### Sensor Entities

`sensor.py` defines descriptions for PM2.5 and filter life. Description
functions read values from the same `GoveeData` instance used by the fan and
controller.

Sensors use normal `CoordinatorEntity` availability. A cached PM2.5 value can
remain in coordinator data after a bad poll, but the entity is unavailable until
the coordinator recovers.

### Custom Auto Switch

`switch.py` represents logical ownership by `CustomAutoController`, not a
physical purifier switch.

- Turning it on calls `controller.async_activate()`.
- Turning it off transactionally hands control to the purifier's hardware Auto
  mode.
- Controller listeners cause immediate Home Assistant state writes when
  ownership changes.

The switch is the sole current owner of Custom Auto restore state. It stores:

- Whether Custom Auto was active.
- The underlying manual speed it controlled.

On upgrade, it can migrate the older restore record stored by the fan entity.
Legacy fan state is accepted only when its explicit `custom_auto_active`
attribute is true; the fan's ordinary `on` state means purifier power and is not
treated as Custom Auto ownership.

`select.py` is retained as inactive older code. It is not listed in `PLATFORMS`
and Home Assistant does not load it.

## Custom Auto Controller

`CustomAutoController` is a policy engine layered above the coordinator. It does
not communicate with BLE directly.

### Controller State

The controller tracks:

- `_active`: whether it owns fan speed.
- `_current_speed`: its last commanded percentage.
- `_timer_tasks`: active delayed downshift timers keyed by target speed.
- `_mature_downshifts`: targets whose delays have elapsed.
- `_waiting_for_successful_update`: retry gate after a command failure.
- `_evaluation_pending` and `_evaluation_task`: coalesced coordinator updates.
- `_lock`: serialization for activation, evaluation, handoff, and deactivation.

### Activation

Activation acquires the controller lock and subscribes to coordinator updates.
It chooses an initial speed from, in order:

1. A valid restored speed.
2. The current known manual mode.
3. The lowest Custom Auto speed, 20% Sleep.

With a valid current PM2.5 sample, a normal activation immediately selects the
required speed. Restore activation preserves the restored speed unless air
quality requires an immediate upward correction. Downshift timers are then
started from the current sample.

If activation fails or is canceled, ownership, speed, listeners, and timers are
rolled back.

### Immediate Upward Changes

Upward changes have no delay. `_speed_for_pm()` starts at 20% and selects each
higher speed whose upward threshold is exceeded.

With defaults:

```text
PM2.5 > 3   -> at least 40%
PM2.5 > 5   -> at least 60%
PM2.5 > 9   -> at least 80%
PM2.5 > 15  -> 100%
```

A new valid sample updates downshift timer eligibility before attempting an
upward command. Therefore, a failed upward command cannot leave timers that were
made invalid by the new high sample.

### Delayed Downward Changes

Each lower target has an independent threshold and delay. A timer starts when a
valid PM2.5 sample is at or below that target's downward threshold.

With defaults:

```text
PM2.5 <= 14 for 5 minutes -> target 80%
PM2.5 <= 9  for 5 minutes -> target 60%
PM2.5 <= 5  for 5 minutes -> target 40%
PM2.5 <= 3  for 7 minutes -> target 20% Sleep
```

Multiple timers can run at once. When one or more mature, the controller chooses
the lowest mature speed below its current speed.

The timer lifecycle is:

```text
valid qualifying sample
        |
        v
pending timer -------- valid non-qualifying sample ------> canceled
        |
        | delay expires
        v
mature target -------- valid non-qualifying sample ------> cleared
        |
        | valid qualifying sample and controller ready
        v
coordinator mode command
```

Invalid PM2.5 samples and temporary poll failures do not reset elapsed timer
progress. Wall-clock time continues. If a timer expires while data is invalid,
the target becomes mature but no command is issued. The next valid sample either
applies the mature target or clears it if the sample no longer qualifies.

### Command Failure Retry Gate

If an automatic speed command fails, the controller retains pending and mature
downshift state and sets `_waiting_for_successful_update`.

Timer completion alone cannot clear this gate. Only a later real coordinator
callback with a successful PM2.5 update permits another evaluation. This avoids
a tight retry loop against an unavailable purifier.

### Transactional Ownership Handoff

`async_handoff()` serializes user commands with controller state changes:

1. Acquire the controller lock.
2. Keep controller ownership and timer state intact.
3. Run the requested coordinator command.
4. On success, deactivate and cancel controller timers.
5. On failure or cancellation, remain active and best-effort reassert the prior
   manual speed.
6. Preserve the original exception for the entity service call.

Because activation and handoff share the same lock, concurrent switch and fan
operations receive a defined order. The integration cannot finish with Custom
Auto logically active while a later hardware Auto command silently wins.

### Deactivation

Explicit deactivation, confirmed physical power-off, and config entry unload
cancel timers, clear mature targets, remove the coordinator listener, and notify
entity listeners.

## Concurrency and Lock Ordering

There are three synchronization levels:

```text
CustomAutoController._lock
            |
            v
GoveeCoordinator._state_lock
            |
            v
GoveeBleClient._lock
```

The ordering is always top to bottom when multiple locks are involved:

- Custom Auto evaluation or handoff may call the coordinator while holding the
  controller lock.
- Coordinator polling and commands call the client while holding the
  coordinator state lock.
- The client holds its lock while connecting and completing a BLE transaction.

There is no awaited path in the reverse direction. Coordinator publication
invokes controller listeners synchronously, but those listeners only schedule a
controller task. They do not await the controller lock from inside the
coordinator call. This is what prevents coordinator-to-controller lock
inversion.

The locks solve different races:

- Client lock: overlapping BLE subscriptions, writes, and response futures.
- Coordinator lock: poll-versus-command and command-versus-command state races.
- Controller lock: automatic evaluation, user handoff, activation, and
  deactivation races.

## Error Propagation and Recovery

Errors are translated at layer boundaries:

```text
ProtocolError / BLE backend error / timeout
                |
                v
       GoveeBleClientError or original error
                |
                v
     UpdateFailed for coordinator polling
                |
                v
 Home Assistant unavailable entity state
```

Command errors follow a different path:

```text
BLE command error
      |
      v
coordinator does not publish confirmed command state
      |
      v
controller rolls back or retains ownership policy
      |
      v
entity raises HomeAssistantError to the service caller
```

Cancellation is treated as a rollback event in controller operations. It is
then re-raised so Home Assistant task cancellation semantics remain intact.

## Diagnostics

`diagnostics.async_get_config_entry_diagnostics()` combines:

- Redacted config entry data.
- Current options.
- Current coordinator state.
- Custom Auto configuration and runtime timer state.

MAC and UUID addresses are completely masked. A BLE-advertised name matching a
profile is reduced to the non-unique profile prefix. Diagnostics expose pending
and mature downshift targets, which is useful when determining why Custom Auto
has or has not changed speed.

## Testing Boundaries

The test suite mirrors the architecture:

- `test_protocol.py`: frame building, matching, validation, and decoding.
- `test_client.py`: BLE transaction sequencing, matching, deadlines, cleanup,
  and locking.
- `test_coordinator_logic.py`: shared state, polling, command publication, and
  coordinator serialization.
- `test_controller.py`: Custom Auto thresholds, timers, retries, cancellation,
  and handoff ordering.
- Entity tests: Home Assistant state mapping, commands, restoration, and
  availability.
- Config flow tests: discovery, validation, duplicate handling, and options.
- Diagnostics and packaging tests: redaction and repository metadata.
- `test_runtime_smoke.py`: imports and entity construction against a real Home
  Assistant installation.

Most tests use focused Home Assistant and BLE substitutes. Runtime smoke tests
verify API compatibility, but physical BLE behavior still requires a real
purifier.

## Extension Guidelines

### Adding a Device Profile

1. Add protocol constants or decoders only where the new device differs.
2. Create a `ModelProfile` with verified UUIDs, commands, and matchers.
3. Register it in `PROFILES`.
4. Add advertisement matching tests.
5. Verify entity capabilities derive correctly from its fan-mode commands.

Do not reuse H7124 response offsets or confirmation rules without captures that
show the new model uses them.

### Adding a Measurement

1. Extend the protocol-decoded model.
2. Decode and semantically validate the new field.
3. Extend `GoveeData` and coordinator merge behavior.
4. Add a sensor description.
5. Decide explicitly whether invalid samples retain cached state and how
   availability should behave.
6. Add diagnostics and tests.

### Adding a Command

1. Define the command and a strict confirmation matcher.
2. Add a client method that runs through the serialized transaction path.
3. Add a coordinator method that publishes only confirmed state.
4. Route entity calls through controller handoff if the command transfers
   ownership away from Custom Auto.
5. Add command failure, timeout, cancellation, and concurrency tests.

### Changing Custom Auto Rules

Threshold changes affect config flow validation, controller evaluation, user
text, diagnostics, and tests. Preserve these invariants:

- Upward action is immediate.
- Downward action requires a mature target and a valid confirming sample.
- Missing data preserves elapsed progress but cannot trigger a command.
- A valid non-qualifying sample clears pending and mature intent.
- Automatic command failure cannot cause a timer-driven retry loop.
- User handoff is serialized with automatic evaluation.

## Important Invariants

When modifying the integration, keep the following properties true:

1. One runtime client, coordinator, and controller exist per config entry.
2. BLE requests for a purifier never overlap.
3. Polls and commands cannot publish state concurrently.
4. State is published only after device confirmation.
5. Entity availability reflects coordinator health, not merely cached values.
6. Invalid PM2.5 data never drives Custom Auto.
7. Timer progress survives temporary missing data.
8. Only a real successful coordinator update opens the command retry gate.
9. User handoff either succeeds and releases ownership or fails while retaining
   ownership and policy state.
10. Config entry unload leaves no controller timers or delayed coordinator
    refresh tasks behind.
11. Persisted identifiers are stable, and diagnostics do not expose them.
12. Model-specific protocol assumptions remain below the coordinator layer.
