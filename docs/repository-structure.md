# Repository Structure

This repository contains a locally polling Home Assistant custom integration for
Govee `H712*` family BLE air purifiers. Only the H7124 is physically tested and
validated; other recognized `H712*` models fall back to the H7124 protocol
definition and may fail or expose unsupported or mismatched features. Home
Assistant loads the code under `custom_components/`; the repository is not a
standalone service.

For runtime interactions and lock ownership, see
[`architecture.md`](architecture.md). For Home Assistant and HACS standards,
support baselines, user-interface layouts, and authoritative external links, see
[`home-assistant-hacs-reference.md`](home-assistant-hacs-reference.md).

## Top-Level Layout

```text
govee-ble-air-purifier/
|-- custom_components/govee_ble_air_purifier/  # Integration package
|-- tests/                                      # Fast and runtime smoke tests
|-- docs/                                       # User-interface and maintainer references
|-- .github/workflows/validate.yml              # CI validation lanes
|-- README.md                                   # Installation and user guide
|-- hacs.json                                   # HACS metadata
`-- pyproject.toml                              # Python, pytest, and Ruff config
```

## Home Assistant Entrypoints

The integration package's root Home Assistant entrypoints are:

- `__init__.py`: config-entry setup, runtime composition, option reload, and
  unload.
- `config_flow.py`: setup and options flows.
- `fan.py`: power, manual speeds, and Manual or hardware Auto presets.
- `sensor.py`: PM2.5 and filter-life sensors.
- `switch.py`: restored logical ownership for integration-managed Custom Auto.
- `diagnostics.py`: redacted config, coordinator, and controller diagnostics.

`entity.py` supplies common coordinator subscription, device information, and
unique IDs. `const.py` declares only `fan`, `sensor`, and `switch` as active
platforms. The former `select` platform has been removed; fan mode belongs to
the fan entity.

`manifest.json`, `strings.json`, and `translations/en.json` provide Home
Assistant metadata and user-facing text. `setup_helpers.py` contains address,
cached-advertisement, device-option, and polling-interval helpers shared by
configuration flows.

## Protocol And Bluetooth

```text
models.py
protocol.py
profiles.py
model_profiles/
|-- default.json
`-- h7124.json
bluetooth/
|-- __init__.py
|-- framing.py
|-- client.py
`-- transport.py
```

- `models.py` defines `DecodedStatus`, the output of one decoded status frame
  (an `aa19` frame for the tested H7124 definition), and `PurifierState`, the
  application-facing snapshot shared above the protocol layer.
- `model_profiles/` holds complete per-model JSON definitions. Each file owns
  the model's GATT service and characteristic UUIDs and the exact outbound
  20-byte power, query, and fan-mode command frames. `default.json` and
  `h7124.json` initially contain the physically tested H7124 definition.
  Future model files are complete definitions, not partial inheritance over
  another file.
- Root `protocol.py` retains shared frame validation, response and confirmation
  matchers, and status decoding for the recognized family. Models with
  different response semantics or framing require Python changes here; they
  cannot be added by JSON alone.
- `profiles.py` matches advertised names beginning `GVH712` plus one
  alphanumeric model character, selects the exact `model_profiles/<model>.json`
  when present and `default.json` otherwise (H7124 protocol behavior), and
  binds the resolved advertisement prefixes, UUIDs, commands, matchers,
  decoders, and capabilities into `ModelProfile`.
- `bluetooth/framing.py` provides generic 20-byte frame construction, checksum
  validation, and `ProtocolError`.
- `bluetooth/client.py` owns the per-purifier transaction lock, writes,
  notification subscription and cleanup, response futures, matching, and shared
  deadlines. It also retains a healthy connection, handles disconnect callbacks,
  derives adaptive idle release from the polling interval, and serializes that
  release with explicit shutdown.
- `bluetooth/transport.py` owns Home Assistant BLE-device lookup, stale
  connection cleanup before establishment, connection establishment, and
  bounded best-effort disconnect primitives.

## State And Custom Auto

```text
coordinator.py
custom_auto/
|-- __init__.py
|-- config.py
|-- policy.py
`-- controller.py
```

- `coordinator.py` defines `GoveeRuntimeData` and `GoveeCoordinator`. The
  coordinator polls, serializes state-changing work, merges `PurifierState`,
  publishes confirmed commands immediately, and schedules reconciliation
  refreshes.
- `custom_auto/config.py` owns defaults, option parsing and validation, and the
  immutable `CustomAutoConfig`.
- `custom_auto/policy.py` owns pure speed constants, mode mappings, and upward
  PM2.5 speed selection.
- `custom_auto/controller.py` owns activation, restored speed, fresh-sample
  tracking, upshift confirmation, downshift timers, retries, coordinator calls,
  and transactional ownership handoff.

Coordinator publication fans out to entities and, while active, the Custom Auto
controller. The controller can call back through the coordinator to request a
confirmed mode command; it does not call Bluetooth code directly.

## Dependency Shape

Dependencies branch around shared models, profile behavior, and runtime
composition rather than forming one chain. Each arrow below means "depends on"
or, where noted, "calls through the public runtime interface":

```text
protocol.py -------------------> bluetooth/framing.py
       `-----------------------> models.py

profiles.py -------------------> protocol.py + models.py
       `-----------------------> model_profiles/*.json (GATT UUIDs and
                                outbound command frames)

bluetooth/client.py -----------> profiles.py + protocol.py
       |-----------------------> bluetooth/framing.py + models.py
       `-----------------------> bluetooth/transport.py

coordinator.py ----------------> profiles.py + models.py
custom_auto/controller.py -----> custom_auto/config.py + custom_auto/policy.py
       `-- runtime calls ------> coordinator.py

fan.py / sensor.py / switch.py -> entity.py + shared runtime objects
config_flow.py ----------------> profiles.py + setup_helpers.py
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
|   |-- test_framing.py
|   |-- test_client.py
|   `-- test_transport.py
|-- custom_auto/
|   |-- test_config.py
|   |-- test_policy.py
|   `-- test_controller.py
|-- helpers/
|   `-- ha_stubs.py
|-- conftest.py
|-- test_protocol.py
|-- test_coordinator_logic.py
|-- test_fan_entity.py
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
not installed. The separate smoke lane installs real Home Assistant versions
and runs only `test_runtime_smoke.py` to check imports, API inheritance and
signatures, entity construction, config flow, and lifecycle composition.

```bash
python -m pytest --ignore=tests/test_runtime_smoke.py
python -m pytest tests/test_runtime_smoke.py  # requires Home Assistant
```

## Packaging And Validation

- `.github/workflows/validate.yml` runs Ruff, the fast behavioral lane, the
  real-Home-Assistant smoke matrix, HACS validation, and hassfest.
- `hacs.json` describes the HACS repository.
- `pyproject.toml` defines Python compatibility, development dependencies,
  pytest markers, and Ruff settings.
- `brand/icon.png` is the repository icon.
