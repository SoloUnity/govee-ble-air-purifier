# Repository Structure

This repository contains a hybrid push-and-poll Home Assistant custom integration
for Govee `H712*` family BLE air purifiers. It has exact plaintext H7124 and
encrypted H7129 profiles. H7124 commands, polling, and push layouts are physically
validated; H7129
connection, encrypted handshake, polling, disconnect, and recovery are
physically observed, while H7129 state-changing command validation remains
pending and its manual/light push layouts remain inferred. Other recognized
`H712*` models fall back to the H7124 protocol
definition and may fail or expose unsupported or mismatched features. Home
Assistant loads the code under `custom_components/`; the repository is not a
standalone service.

For runtime interactions and lock ownership, see
[`architecture.md`](architecture.md). For Home Assistant and HACS standards,
support baselines, user-interface layouts, and authoritative external links, see
[`home-assistant-hacs-reference.md`](home-assistant-hacs-reference.md). For the
detailed H7124 and H7129 BLE protocol, see
[`govee-ble-air-purifier-protocol.md`](govee-ble-air-purifier-protocol.md).

## Top-Level Layout

```text
govee-ble-air-purifier/
|-- custom_components/govee_ble_air_purifier/  # Integration package
|-- tests/                                      # Fast and runtime smoke tests
|-- docs/                                       # User-interface and maintainer references
|-- .github/workflows/validate.yml              # CI validation lanes
|-- README.md                                   # Installation and user guide
|-- hacs.json                                   # HACS metadata
`-- pyproject.toml                              # Protocol wheel and quality config
```

## Home Assistant Entrypoints

The integration package's root Home Assistant entrypoints are:

- `__init__.py`: config-entry setup, runtime composition, option reload, and
  unload.
- `config_flow.py`: setup and options flows.
- `setup_probe.py`: bounded read-only GATT/protocol validation before entry
  creation.
- `auto_resume.py`: persisted hardware-Auto and Custom-Auto selection,
  suspension, and power-on reconciliation.
- `fan.py`: power, manual speeds, and Manual or hardware Auto presets.
- `light.py`: profile-gated night-light power, brightness, and RGB control.
- `sensor.py`: PM2.5 and filter-life sensors.
- `switch.py`: selected and active state for integration-managed Custom Auto.
- `diagnostics.py`: redacted config, coordinator, and controller diagnostics.

`entity.py` supplies common coordinator subscription, device information, and
unique IDs. `const.py` declares `fan`, `light`, `sensor`, and `switch` as active
platforms. The former `select` platform has been removed; fan mode belongs to
the fan entity.

`manifest.json`, `strings.json`, and `translations/en.json` provide Home
Assistant metadata and user-facing text. `setup_helpers.py` contains address,
cached-advertisement, device-option, and polling-interval helpers shared by
configuration flows.

## Protocol And Bluetooth

```text
govee_ble_air_purifier_protocol/
|-- __init__.py
|-- models.py
|-- protocol.py
|-- profiles.py
|-- framing.py
|-- govee_v1.py
|-- py.typed
|-- model_profiles/
|   |-- default.json
|   |-- h7124.json
|   `-- h7129.json
`-- schemas/model_profile_v5.schema.json
models.py / profiles.py / protocol.py          # Compatibility facades
bluetooth/
|-- __init__.py
|-- _arbiter.py
|-- _connection.py
|-- _night_light_polling.py
|-- _notifications.py
|-- _push.py
|-- _session.py
|-- _transactions.py
|-- framing.py                                 # Compatibility facade
|-- govee_v1.py                                # Compatibility facade
|-- client.py
`-- transport.py
```

- The reusable package's `models.py` defines `DecodedStatus`, the output of one decoded status frame
  (an `aa19` frame for the tested H7124 definition), `NightLightState`,
  `PurifierState`, the application-facing snapshot shared above the protocol
  layer, and the partial internal `PurifierPushUpdate`.
- Its `model_profiles/` holds complete per-model JSON definitions. Each file owns
  the model's GATT service and characteristic UUIDs and the exact outbound
  20-byte power, query, and fan-mode command frames plus the transport encryption
  mode. H7124 and H7129 additionally contain an optional `night_light` block
  with static calls, variable brightness/RGB templates, and a polling policy.
  H7124 selects five-minute sequential reconciliation with exponential backoff;
  H7129 explicitly selects its existing every-poll pipelined behavior.
  `default.json` omits the block, preventing unverified fallback models from
  exposing a light. Schema 5 includes an explicit model support status, the
  polling policy, and optional push-capability flags. H7124 and H7129 enable power,
  fan-mode, and night-light power/brightness pushes; `default.json` leaves them
  disabled.
  Future model files are complete definitions, not partial inheritance over
  another file.
- The package's `protocol.py` retains shared frame validation, response and confirmation
  matchers, and status decoding for the recognized family. Models with
  different response semantics or framing require Python changes here; they
  cannot be added by JSON alone.
- The package's `profiles.py` searches advertised names case-insensitively for `H712` plus one
  ASCII letter or digit, selects the exact
  `model_profiles/<model>.json` when present and `default.json` otherwise
  (H7124 protocol behavior), and binds the resolved advertisement identity,
  UUIDs, commands, matchers, decoders, and capabilities into `ModelProfile`.
- The package's `framing.py` provides generic 20-byte frame construction, checksum
  validation, and `ProtocolError`.
- The package's `govee_v1.py` provides the captured AES/RC4-compatible frame
  transform and handshake frame helpers without owning model commands.
- `bluetooth/client.py` owns the per-purifier transaction lock, integration
  connection lease, read-only retry policy, and Home Assistant integration
  callbacks. `bluetooth/_connection.py` owns retained-connection establishment,
  reuse, invalidation, cleanup, and notification recovery without knowing
  command or replay semantics. `bluetooth/_session.py` owns exact-client identity,
  connection generations, disconnect signalling, application encoding,
  encrypted-session keys, notification ownership, and cancellation quarantine.
  `bluetooth/_transactions.py` owns typed required/optional exchange plans,
  temporary response routes, response futures, matching, diagnostics, and
  application phase deadlines without knowing how to establish or replay a
  connection. The client also coordinates one retained connection across entries
  that opt into sharing, while default dedicated entries retain their own;
  reuses connections for same-device activity; uses an exact-client
  disconnect signal to wake pending waits, waits for fresh post-disconnect
  advertisements, retries one read-only poll, negotiates and clears
  connection-specific encrypted sessions when selected by the profile, derives
  adaptive idle release from the polling interval, and serializes that release
  with explicit shutdown. The shared lease prioritizes queued controls while
  bounding command bursts so routine polls cannot starve. Dedicated push-enabled
  clients retain their connection regardless of polling interval; shared clients
  receive pushes only while they own the slot. Lifecycle logs use a stable short
  hashed device label. Timed-out platform operations remain observed and
  quarantined on their replaced client without blocking a fresh connection
  generation; notification recovery has bounded lock and cleanup stages.
- `bluetooth/transport.py` owns Home Assistant advertisement and per-scanner
  path preparation, best-effort Home Assistant 2026.6+ temporary
  Automatic-to-Active requests only during new-advertisement waits, BLE-device
  lookup, stale connection cleanup before establishment, bounded connection
  attempts, and cancellation-independent bounded best-effort stale cleanup and
  disconnect primitives.

## State And Custom Auto

```text
auto_resume.py
coordinator.py
custom_auto/
|-- __init__.py
|-- config.py
|-- policy.py
`-- controller.py
```

- `auto_resume.py` owns the shared automatic-mode intent, serialization of
  explicit mode changes, physical power-transition reconciliation, physical
  mode ownership overrides, and resume retries. The fan and Custom Auto entities
  replicate its persisted attributes; startup restores the newest available
  record so either entity may be disabled.
- `coordinator.py` defines `GoveeRuntimeData` and `GoveeCoordinator`. The
  coordinator polls, serializes state-changing work without blocking controls
  behind waiting polls, merges `PurifierState`, publishes confirmed commands
  and pushed partial state immediately, tracks command, push-field, and device-
  observation revisions, owns push publication tasks, and schedules reconciliation
  refreshes.
- `custom_auto/config.py` combines model-profile boundaries with shared timing
  defaults and owns option parsing, validation, and immutable `CustomAutoConfig`.
- `custom_auto/policy.py` owns pure speed and confirmation constants, mode
  mappings, and upward PM2.5 speed selection.
- `custom_auto/controller.py` owns activation, restored speed, fresh-sample
  tracking, upshift confirmation and its one-shot delayed full-state poll,
  downshift timers, retries, coordinator calls, and transactional ownership
  handoff.

Coordinator publication fans out to entities, the Auto resume manager, and,
while active, the Custom Auto controller. Both runtime controllers call through
the coordinator for confirmed commands; neither calls Bluetooth code directly.

## Dependency Shape

Dependencies branch around shared models, profile behavior, and runtime
composition rather than forming one chain. Each arrow below means "depends on"
or, where noted, "calls through the public runtime interface":

```text
reusable protocol package -----> framing + crypto + models + protocol
       `-----------------------> model_profiles/*.json + JSON Schema

bluetooth/client.py -----------> reusable protocol package
       `-----------------------> bluetooth/transport.py

coordinator.py ----------------> reusable protocol package
custom_auto/controller.py -----> custom_auto/config.py + custom_auto/policy.py
       `-- runtime calls ------> coordinator.py

fan.py / light.py / sensor.py / switch.py -> entity.py + shared runtime objects
config_flow.py ----------------> reusable protocol package + setup_helpers.py
       `-----------------------> custom_auto/config.py

__init__.py composes client + coordinator + profile + Custom Auto + platforms
diagnostics.py reads config entry + coordinator + controller
```

The diagram shows important runtime dependencies, not every import. Generic
framing is below the shared protocol code; JSON model definitions are below the
profile layer; transport is below the transaction client; and pure Custom Auto
config and policy are below its mutable controller.

## Tests

```text
tests/
|-- bluetooth/
|   |-- test_client.py
|   |-- test_encrypted_client.py
|   |-- test_session.py
|   |-- test_transactions.py
|   |-- test_framing.py
|   |-- test_govee_v1.py
|   `-- test_transport.py
|-- custom_auto/
|   |-- test_config.py
|   |-- test_policy.py
|   `-- test_controller.py
|-- helpers/
|   `-- ha_stubs.py
|-- conftest.py
|-- scan_bluetooth.py
|-- test_protocol.py
|-- test_coordinator_logic.py
|-- test_fan_entity.py
|-- test_light_entity.py
|-- test_sensor_entities.py
|-- test_switch_entity.py
|-- test_config_flow_logic.py
|-- test_config_flow_runtime.py
|-- test_init_lifecycle.py
|-- test_diagnostics.py
|-- test_persistence_contracts.py
|-- test_hacs_packaging.py
`-- test_runtime_smoke.py
```

The fast lane excludes `test_runtime_smoke.py` and uses focused substitutes;
`tests/conftest.py` supplies a minimal coordinator stub when Home Assistant is
not installed. `scan_bluetooth.py` is a manually run, passive BLE advertisement
scanner and is not collected by pytest. The separate smoke lane installs real
Home Assistant versions and runs only `test_runtime_smoke.py` to check imports,
API inheritance and signatures, entity construction, config flow, and lifecycle
composition. Its matrix includes Home Assistant 2026.5 and 2026.6 separately
because advertisement-history clearing and on-demand Active scan requests were
introduced in different releases.

```bash
python -m pytest --ignore=tests/test_runtime_smoke.py
python -m pytest tests/test_runtime_smoke.py  # requires Home Assistant
```

## Packaging And Validation

- `.github/workflows/validate.yml` runs Ruff, the fast behavioral lane, the
  real-Home-Assistant smoke matrix, HACS validation, and hassfest.
- `hacs.json` describes the HACS repository.
- `pyproject.toml` maps the nested reusable source to the independently
  buildable `govee_ble_air_purifier_protocol` package, includes its JSON and
  typing data, and defines Python compatibility, dependencies, tests, coverage,
  Ruff, and strict mypy settings.
- `brand/icon.png` is the repository icon.
