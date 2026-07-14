# Repository Structure

This repository contains a Home Assistant custom integration for monitoring and
controlling Govee H7124-style air purifiers over Bluetooth Low Energy (BLE).
The integration communicates locally with the purifier and does not require a
cloud service.

## Top-Level Layout

```text
govee-ble-air-purifier/
|-- custom_components/govee_ble_air_purifier/  # Home Assistant integration
|-- tests/                                      # Unit test suite
|-- docs/                                       # Project documentation
|-- .github/workflows/                          # Repository validation in CI
|-- README.md                                   # Installation and user guide
|-- hacs.json                                   # HACS repository metadata
`-- pyproject.toml                              # Python and test configuration
```

The application code lives under `custom_components/`, following Home
Assistant's custom integration layout. The repository itself is not a
standalone service or application; Home Assistant loads and runs the component.

## Integration Modules

The integration is divided into layers, with BLE protocol details at the bottom
and Home Assistant entities at the top.

### Integration Lifecycle

- `__init__.py` creates the BLE client and data coordinator when Home Assistant
  loads a config entry. It also loads and unloads the supported entity
  platforms.
- `const.py` defines the integration domain, configuration keys, polling limits,
  and the active platforms.
- `manifest.json` declares the integration to Home Assistant, including its
  runtime requirements and local-polling behavior.

### Setup and Configuration

- `config_flow.py` implements initial device setup and the options flow.
- `setup_helpers.py` contains device-discovery formatting, address handling, and
  polling-interval validation that can be tested independently of Home
  Assistant.
- `strings.json` and `translations/en.json` provide text shown in the Home
  Assistant interface.

Setup is initiated manually from Home Assistant. The flow can present compatible
devices already visible to Home Assistant's Bluetooth stack, or accept a BLE
address directly.

### BLE Protocol and Device Support

- `models.py` defines the decoded purifier state returned by the protocol layer.
- `protocol.py` builds and validates BLE frames, defines commands, and decodes
  power, air-quality, filter-life, and fan-mode responses.
- `profiles.py` groups model-specific UUIDs, commands, and decoder functions into
  device profiles. The H7124 profile is currently the supported profile and is
  the main extension point for adding other models.

These modules contain most of the device-specific knowledge and are kept
separate from Home Assistant entity behavior.

### Communication and State

- `client.py` manages BLE connections, writes commands, subscribes to
  notifications, and waits for matching responses. BLE operations are
  serialized to avoid overlapping access to a device.
- `coordinator.py` periodically polls the client and owns the shared state used
  by all entities. Commands publish confirmed state promptly and schedule a
  follow-up refresh to reconcile the integration with the physical device.

### Home Assistant Entities

- `entity.py` provides common device information and unique-ID behavior.
- `fan.py` exposes power, manual fan speeds, and automatic/manual preset modes.
- `sensor.py` exposes PM2.5 and remaining filter-life measurements.
- `switch.py` and `select.py` contain older power and mode entity implementations
  but are not currently loaded. Their functionality is represented by the fan
  entity.
- `diagnostics.py` returns redacted configuration and current state for Home
  Assistant diagnostics.

The active platforms are `fan` and `sensor`, as declared in `const.py`.

## Runtime Data Flow

```text
Home Assistant config entry
          |
          v
     __init__.py
          |
          |-- creates GoveeBleClient
          `-- creates GoveeCoordinator
                         |
                         v
                  BLE request/response
                         |
                         v
                    Purifier state
                         |
                         v
              Fan and sensor entities
```

On startup, the integration resolves the configured device profile, creates a
BLE client, creates a coordinator, and performs an initial refresh. The
coordinator then polls at the configured interval and publishes state changes to
the entities.

Control commands travel in the opposite direction: an entity asks the
coordinator to change power or fan mode, the coordinator delegates to the BLE
client, and the client waits for confirmation from the purifier before the new
state is published.

## Internal Dependency Direction

The primary dependency direction is:

```text
models.py
    |
    v
protocol.py
    |
    v
profiles.py
    |
    v
client.py
    |
    v
coordinator.py
    |
    v
entity.py
    |
    v
fan.py / sensor.py
```

`config_flow.py` uses `profiles.py` and `setup_helpers.py` to create config
entries. `__init__.py` ties the client, coordinator, and entity platforms
together at runtime.

## Tests

The `tests/` directory contains unit tests for the protocol, BLE client,
coordinator, setup flow, entities, diagnostics, and packaging metadata. Tests
replace Home Assistant and BLE runtime objects with focused stubs, allowing most
behavior to be checked without running Home Assistant or connecting to a real
purifier.

The usual local test command is:

```bash
python -m pytest -q
```

## Packaging and Validation

- `hacs.json` describes the repository to the Home Assistant Community Store.
- `pyproject.toml` defines Python requirements, pytest settings, and Ruff
  settings.
- `.github/workflows/validate.yml` runs HACS and Home Assistant integration
  validation for pushes and pull requests.
- `brand/icon.png` contains the integration icon used by HACS.

## Common Extension Points

- Add support for another purifier family by defining its protocol behavior and
  registering another profile in `profiles.py`.
- Add another Home Assistant entity by creating a platform module and adding the
  platform name to `PLATFORMS` in `const.py`.
- Add another measured value by extending the decoded state, coordinator data,
  and sensor descriptions together.
