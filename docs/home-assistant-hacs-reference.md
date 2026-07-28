# Home Assistant And HACS Reference

This document records the Home Assistant and HACS contracts used by this
repository. It also maps the user interface for installing the integration,
adding a purifier, changing its settings, and finding its entities.

This is both a user-interface reference and a maintainer checklist. Runtime
design and lock ownership are documented separately in
[`architecture.md`](architecture.md), while the file map is in
[`repository-structure.md`](repository-structure.md). The detailed BLE protocol
reference is
[`govee-ble-air-purifier-protocol.md`](govee-ble-air-purifier-protocol.md).

## Product And Support Baseline

| Item | Repository contract | Source of truth |
| --- | --- | --- |
| Integration name | Govee BLE Air Purifier | `manifest.json` |
| Domain | `govee_ble_air_purifier` | `manifest.json` and `const.py` |
| Integration release | `0.1.0` | `manifest.json` |
| Home Assistant minimum | `2024.8.0` | `hacs.json` |
| HACS minimum | `1.34.0` | `hacs.json` |
| Python package baseline | Python 3.12 or newer | `pyproject.toml` |
| Recognized model family | Govee `H712*` BLE purifiers | `profiles.py` and `model_profiles/` |
| Exact model profiles | Plaintext H7124 and encrypted H7129 | `model_profiles/h7124.json` and `model_profiles/h7129.json` |
| Physical integration validation | H7124 commands and polling; H7129 connection, encrypted handshake, polling, disconnect, and recovery | `model_profiles/h7124.json`, `model_profiles/h7129.json`, and field evidence |
| Recognized BLE local name | Contains `H712` followed by one ASCII letter or digit, case-insensitively (for example `GVH7124`, `GVH712C`, or `ihoment_H7129_6A7D`) | `profiles.py` |
| Home Assistant integration type | `device` | `manifest.json` |
| Home Assistant IoT class | `local_polling` | `manifest.json` |
| Active entity platforms | `fan`, `sensor`, `switch` | `const.py` |
| Configuration method | Home Assistant config entries and options flow | `config_flow.py` |
| Distribution method | HACS custom integration repository | `hacs.json` and repository layout |

The integration communicates directly over Bluetooth Low Energy. It does not
use the Govee cloud, YAML configuration, Matter, Zigbee, Z-Wave, or MQTT.

The H7124 integration is physically tested and validated. H7129 support is
implemented from decrypted physical-device captures and tested against fixed
encrypted vectors and simulated connection lifecycles. Physical H7129 evidence
confirms connection, encrypted handshake, polling, disconnect, and recovery;
state-changing commands have not yet been physically validated through this
integration. Other recognized `H712*` models without an exact
`model_profiles/<model>.json` file use `default.json`, which supplies the tested
H7124 protocol behavior. Those fallback models remain unverified.

The CI runtime matrix tests the minimum Home Assistant release, relevant API
boundaries, and the current target selected by the repository. At the time of
writing those targets are
Home Assistant `2024.8.0` on Python 3.12, the 2026.5 and 2026.6 Bluetooth API
boundaries, and Home Assistant `2026.7.2` on Python 3.14.2. The workflow is the
source of truth when the current target changes.

## HACS And Home Assistant Responsibilities

HACS and Home Assistant perform different jobs:

| System | Responsibility |
| --- | --- |
| HACS | Downloads, installs, and updates the integration package under Home Assistant's `custom_components/` directory. |
| Home Assistant | Runs the integration, discovers reachable BLE devices, creates config entries, registers devices and entities, stores options, and exposes diagnostics. |

Installing the repository in HACS does not add a purifier. After downloading
or updating the package, restart Home Assistant when HACS requests it, then use
Home Assistant's **Add integration** flow.

## HACS Installation Layout

This repository is currently installed as a custom HACS repository:

```text
HACS
`-- top-right three-dot menu
    `-- Custom repositories
        |-- Repository: https://github.com/SoloUnity/govee-ble-air-purifier
        |-- Type: Integration
        `-- Add
```

After adding the repository:

```text
HACS
`-- Search: Govee BLE Air Purifier
    `-- Download
        `-- Restart Home Assistant when requested
```

HACS installs this runtime directory:

```text
<home-assistant-config>/
`-- custom_components/
    `-- govee_ble_air_purifier/
        |-- __init__.py
        |-- manifest.json
        |-- config_flow.py
        |-- strings.json
        |-- translations/
        |-- bluetooth/
        |-- custom_auto/
        |-- model_profiles/
        `-- active entity and support modules
```

The HACS packaging contract used here is:

- One integration exists under `custom_components/`.
- Every runtime file is contained by
  `custom_components/govee_ble_air_purifier/`.
- Root `hacs.json` supplies the display name and minimum Home Assistant and
  HACS versions.
- `manifest.json` supplies the domain, name, version, documentation URL, issue
  tracker, code owner, integration type, IoT class, and dependencies.
- `brand/icon.png` supplies the repository icon required by HACS.
- `content_in_root` is not enabled because the standard
  `custom_components/<domain>/` layout is used.
- GitHub releases are preferred by HACS but are not required. If releases are
  published, the integration version in `manifest.json`, release tag, and
  release notes should describe the same release.

## Add Device Menu

Home Assistant labels the action **Add integration**. Completing this flow
creates one config entry, one device, and the entities for one purifier.

Use this path:

```text
Settings
`-- Devices & services
    `-- Integrations
        `-- Add integration
            `-- Search: Govee BLE Air Purifier
```

The flow is manually initiated. The manifest does not register an automatic
Bluetooth discovery flow. Opening the first form requests a one-shot active
scan when the installed Home Assistant version provides that API, then reads
Home Assistant's cache of connectable Bluetooth advertisements.

### Step 1: Govee BLE Air Purifier

```text
Govee BLE Air Purifier
|-- Recently seen purifier
|   |-- <recognized names containing H712 plus one ASCII letter or digit>
|   `-- Enter address manually
|-- BLE address for manual setup
|-- Name
|-- Polling interval in seconds
`-- Submit
```

The fields behave as follows:

| Field | Required behavior |
| --- | --- |
| Recently seen purifier | Defaults to the first compatible cached device, or **Enter address manually** when none is available. |
| BLE address for manual setup | Required only for manual entry. Accepts a complete MAC address or platform BLE UUID. |
| Name | Defaults to the matched profile's display name (`Govee H7124 Air Purifier` for the tested H7124 definition); a discovered device name is used when available unless the user supplies a name. |
| Polling interval | Whole seconds from 5 through 300; default is 10. |

Manual address entry is not blind. Home Assistant must have cached a
connectable advertisement for that address whose name contains the recognized
family token: `H712` followed by one ASCII letter or digit,
case-insensitively. The purifier does not have to remain visible at the instant
the form is submitted if Home Assistant still has compatible advertisement
history.

The normalized BLE address is the config entry's unique ID, which prevents the
same purifier from being configured twice. Address, name, and model profile are
stored as config entry data. The stored profile is the exact detected lowercase
model key such as `h7126`, even when `default.json` supplies the behavior, so
an exact model file added later takes effect after an update and restart.
Existing entries that predate the profile key and existing `h7124` entries
resolve to H7124. Polling and, when supported, Custom Auto values are mutable
options.

### Step 2: Five Air Quality Steps

When the resolved profile defines Sleep, Low, Medium, High, Turbo, and hardware
Auto, setup displays four expanded sections after the device form passes
validation:

```text
Five Air Quality Steps
|-- Excellent to Good (20% / 40%)
|   |-- Increase to Good above
|   |-- Return to Excellent at or below
|   `-- Time at or below before returning
|-- Good to Fair (40% / 60%)
|   |-- Increase to Fair above
|   |-- Return to Good at or below
|   `-- Time at or below before returning
|-- Fair to Bad (60% / 80%)
|   |-- Increase to Bad above
|   |-- Return to Fair at or below
|   `-- Time at or below before returning
|-- Bad to Poor (80% / 100%)
|   |-- Increase to Poor above
|   |-- Return to Bad at or below
|   `-- Time at or below before returning
`-- Submit
```

PM2.5 inputs are whole numbers from 0 through 999 micrograms per cubic meter.
Delay inputs are whole minutes from 0 through 1440. The defaults are:

| Boundary | Increase above | Return at or below | Return delay | Speeds |
| --- | ---: | ---: | ---: | --- |
| Excellent / Good | 3 | 3 | 7 minutes | 20% / 40% |
| Good / Fair | 5 | 5 | 5 minutes | 40% / 60% |
| Fair / Bad | 9 | 9 | 5 minutes | 60% / 80% |
| Bad / Poor | 15 | 15 | 5 minutes | 80% / 100% |

The four increase values must be strictly ascending, the four return values
must be strictly ascending, and a return value cannot exceed the corresponding
increase value. An increase has no time delay but requires two distinct valid
PM2.5 samples that both require an upshift. A return qualifies at equality and
occurs only after its configured delay.

Submitting this step creates and loads the config entry. Profiles with narrower
fan-mode sets skip this step and create the entry directly from the device form.
A failed first BLE refresh prevents entities from being created until Home
Assistant can set up the entry successfully.

## Device And Entity Layout

Find the device with either path:

```text
Settings > Devices & services > Devices > <purifier name>
```

```text
Settings > Devices & services > Integrations
  > Govee BLE Air Purifier > <config entry or device>
```

One config entry registers one Home Assistant device with manufacturer `Govee`
and the exact detected model reported by its resolved profile (for example
`H7126`, even when that model uses `default.json`). The device contains these
entities:

| Entity | Home Assistant domain | Purpose |
| --- | --- | --- |
| Purifier | `fan` | Power, percentage, and Manual or Auto preset control. |
| PM2.5 | `sensor` | PM2.5 measurement in micrograms per cubic meter with the Home Assistant PM2.5 device class and measurement state class. |
| Filter life | `sensor` | Remaining filter percentage with the measurement state class. |
| Custom Auto | `switch` | Gives integration-managed PM2.5 rules ownership of fan speed when the profile defines all policy modes. |

The fan maps percentages to Sleep 20%, Low 40%, Medium 60%, High 80%, and
Turbo 100%. Its **Auto** preset selects the purifier's built-in hardware Auto
mode. The **Custom Auto** switch is separate: it makes Home Assistant apply the
configured five-level policy while the fan reports logical Auto.

Selecting a manual percentage or Manual preset hands control away from Custom
Auto. Turning the purifier off suspends either selected Auto mode; Home
Assistant restores that intent across restarts and resumes it after a Home
Assistant or physical-button power-on. Custom Auto remains selected while
suspended. Turning Custom Auto off hands the purifier to hardware Auto when on,
or changes the resume target to hardware Auto without powering on an off
purifier.

Entity unique IDs are derived from the stable config-entry unique ID plus
`fan`, `pm25`, `filter_life`, or `custom_auto`. These values are persistence
contracts and must not be renamed casually because Home Assistant's entity
registry and user automations depend on them.

## Integration Settings Menu

Open the options flow from the integration entry:

```text
Settings
`-- Devices & services
    `-- Integrations
        `-- Govee BLE Air Purifier
            `-- Configure
```

Depending on the Home Assistant frontend version, **Configure** may appear as a
button on the entry or in its overflow menu. It opens one form:

```text
Purifier Options
|-- Polling interval in seconds
|-- Excellent to Good (20% / 40%) [when Custom Auto is supported]
|-- Good to Fair (40% / 60%) [when Custom Auto is supported]
|-- Fair to Bad (60% / 80%) [when Custom Auto is supported]
|-- Bad to Poor (80% / 100%) [when Custom Auto is supported]
`-- Submit
```

For profiles that support Custom Auto, each air-quality section contains the
same increase, return, and delay fields shown during setup. Existing values are
prefilled and the same ranges and ordering rules apply. Narrower profiles show
only polling. Saving options reloads the config entry so the polling interval
and any Custom Auto controller use the new settings consistently.

The BLE address, stored device name, and model profile are setup data and are
not edited by this options form. To replace the physical device, remove the
config entry and add the intended purifier.

## Diagnostics Menu

Home Assistant exposes config-entry diagnostics from the integration entry's
options or overflow menu:

```text
Settings > Devices & services > Integrations
  > Govee BLE Air Purifier > Download diagnostics
```

The exact menu placement can vary by frontend release. The device page may also
offer the config-entry diagnostics when device-specific diagnostics are not
implemented.

Diagnostics include config entry data, options, the current purifier snapshot,
and Custom Auto controller state. The integration always redacts the stored
name and BLE address. New diagnostic fields must follow Home Assistant's rule
that credentials, personal information, addresses, and other sensitive data
must not be exposed.

## Home Assistant Standards Used

### Custom Integration Structure

- Runtime code lives under `custom_components/govee_ble_air_purifier/`.
- `manifest.json` declares a version because this is a custom integration.
- The manifest domain matches both the directory and `DOMAIN` constant.
- `config_flow: true` declares UI-based config-entry setup.
- `integration_type: device` means one physical purifier is represented by one
  config entry.
- `iot_class: local_polling` describes direct local communication with periodic
  reads.
- `bluetooth_adapters` is a Home Assistant integration dependency.

### Config Entries And Data Entry Flows

- `config_flow.py` implements a version 1 multi-step `ConfigFlow` and an
  `OptionsFlow`.
- Voluptuous schemas define structural validation and Home Assistant number
  selectors provide bounded PM2.5 and delay inputs.
- Home Assistant data-entry sections group each boundary and are expanded by
  default.
- A normalized BLE address provides the stable unique ID and duplicate guard.
- Setup data identifies the physical device; options hold mutable behavior.
- `strings.json` and `translations/en.json` define all visible flow labels,
  descriptions, errors, section names, and translated entity names.

### Runtime And Polling

- `async_setup_entry` creates runtime objects and stores them on
  `ConfigEntry.runtime_data`.
- A `DataUpdateCoordinator` performs one shared poll for all entities.
- `async_config_entry_first_refresh()` verifies communication before platform
  entities are forwarded.
- `CoordinatorEntity` supplies coordinator subscriptions and availability.
- Confirmed commands publish updated state and schedule reconciliation with the
  physical purifier.
- Unload stops controller tasks and coordinator work after entity platforms
  unload successfully.

### Devices And Entities

- Device registry identifiers use the integration domain and stable config
  entry unique ID.
- Every entity has a stable unique ID and `has_entity_name = True` behavior.
- The main fan entity uses the device name; secondary sensors and the switch use
  translated entity names.
- The PM2.5 sensor uses Home Assistant's PM2.5 device class, concentration unit,
  and measurement state class.
- The fan declares only supported Home Assistant fan features: speed and preset
  modes, plus explicit turn-on and turn-off features where the installed Home
  Assistant version defines those flags.
- Entity properties read from coordinator/controller memory and do not perform
  BLE I/O.

### Bluetooth

- Device selection uses Home Assistant's central Bluetooth cache rather than
  starting a private scanner.
- Config flow discovery requests Home Assistant's one-shot active scan API when
  available.
- Runtime connections resolve a connectable `BLEDevice` through Home Assistant
  for the configured address, allowing local adapters and compatible Bluetooth
  proxies to participate.
- An uncached runtime connection verifies the per-scanner path first. After a
  disconnect, it requires a newer advertisement and clears static advertisement
  deduplication where Home Assistant supports it. Home Assistant 2026.6 and
  newer can best-effort request a temporary Automatic-to-Active window, subject
  to upstream duration clamping, only while waiting for a new advertisement.
  Cached initial and fresh post-disconnect paths do not request that switch.
  Older installations may require an explicitly Active scanner when active
  discovery is needed.
- Transactions are asynchronous and serialized per purifier so commands and
  polls cannot overlap.
- A healthy GATT connection is reused. Successful activity resets an adaptive
  idle timeout; unexpected disconnects and failed transactions clear it, and
  the next poll or command reconnects through Home Assistant.
- Polling intervals from 5 through 25 seconds retain the connection for the
  interval plus a 5-second margin, up to 30 seconds. Longer intervals retain it
  for only a 5-second command-and-refresh grace before release. Entry unload
  always closes it.
- GATT notifications provide query responses and command confirmation.
- Connection establishment, encrypted negotiation, and application responses
  use separate bounded deadlines, so a slow BlueZ connection does not consume
  the poll or command response budget.
- A confirmed disconnect may retry one idempotent state poll after reconnecting
  and renegotiating encryption. Commands are not replayed automatically.

### Diagnostics, Localization, And Quality

- Config-entry diagnostics are implemented and sensitive identifiers are
  redacted.
- English source strings and the bundled English translation remain identical;
  additional locales belong under `translations/`.
- User-facing names use Home Assistant's entity naming and translation model.
- CI runs Ruff, behavioral tests, real-Home-Assistant smoke tests, HACS
  validation, and hassfest.
- This repository follows relevant Integration Quality Scale guidance but does
  not declare a `quality_scale` in its manifest and has not been assigned a
  Home Assistant Core quality tier. It remains a community custom integration.

## BLE And Device Protocol Specification

Bluetooth Low Energy GATT is the transport standard. The application protocol
above GATT is Govee `H712*` family behavior encoded by this repository. The
H7124 implementation is physically tested and validated. H7129 behavior was
captured from a physical device and decrypted; its encrypted session is now
implemented and covered by captured-vector and simulated-client tests. Physical
integration evidence confirms H7129 connection, encrypted handshake, polling,
disconnect, and recovery, while state-changing command validation remains
pending. This is not presented as an official public Govee specification.

The detailed command, response, capture-evidence, and encrypted-transport
reference is
[`govee-ble-air-purifier-protocol.md`](govee-ble-air-purifier-protocol.md).

The values below document the tested H7124 definition stored in
`model_profiles/h7124.json` and `model_profiles/default.json`:

| Item | Tested H7124 profile value |
| --- | --- |
| Profile key | `h7124` (other `h712?` keys fall back to `default.json`) |
| Advertised name token | `H712` plus one ASCII letter or digit, anywhere in the name and case-insensitive |
| Service UUID | `00010203-0405-0607-0809-0a0b0c0d1910` |
| Notify characteristic | `00010203-0405-0607-0809-0a0b0c0d2b10` |
| Write characteristic | `00010203-0405-0607-0809-0a0b0c0d2b11` |
| Frame size | 20 bytes |
| Checksum | XOR of bytes 0 through 18 stored in byte 19 |
| State query prefix | `aa 01` |
| Status query prefix | `aa 19` |
| Power command prefix | `33 01` |
| Fan mode command prefix | `3a 05` |

The status response supplies PM2.5 and filter life. PM2.5 values above 999 are
treated as invalid rather than published as measurements. The supported fan
commands are Sleep, Low, Medium, High, Auto, and Turbo. The GATT UUIDs and the
exact outbound 20-byte power, query, and fan-mode frames are defined per model
in `model_profiles/*.json`; response markers, decoder offsets, confirmation
rules, and status decoding remain in `protocol.py`; profile selection and
capabilities are defined in `profiles.py`; generic frame length and checksum
validation are defined in `bluetooth/framing.py`.

### Model Profile Definitions

Each file in `model_profiles/` is a complete schema-v1 definition of one model's
transport encryption mode, GATT UUIDs, and outbound command frames. Selection
loads the exact lowercase model file when it exists (including `h7129.json`);
any other recognized `H712*` model falls back to `default.json`, which is the
tested plaintext H7124 definition. A future model file is a complete definition,
not partial inheritance over another file.

The fan entity adapts to the modes listed in the resolved profile. The Custom
Auto switch is created only when that list includes Sleep, Low, Medium, High,
Turbo, and hardware Auto; profiles with narrower mode sets remain usable for
their declared fan commands without exposing an incompatible policy switch.

JSON ownership stops at transport selection, outbound frames, and UUIDs. The
Govee V1 transform stays in `bluetooth/govee_v1.py`; shared frame validation,
response matching, command confirmation, and status decoding stay in
`protocol.py`. H7124 and H7129 therefore reuse the same application protocol
after the client decrypts H7129 notifications.

## Persisted Contracts

Home Assistant stores config entry data, options, entity registry records, and
restored entity state across integration upgrades. The repository therefore
treats these values as compatibility contracts:

- Domain `govee_ble_air_purifier`.
- Profile key is the exact detected lowercase model key such as `h7124` or
  `h7126`. A stored key may not have an exact `model_profiles/<model>.json`
  file yet; `default.json` supplies its behavior until one ships. Existing
  entries that predate the profile key and existing `h7124` entries resolve to
  H7124.
- Config entry keys `address`, `name`, and `profile`.
- Option key `polling_interval` and all `custom_auto_*` threshold and delay
  keys.
- Entity unique-ID suffixes `fan`, `pm25`, `filter_life`, and `custom_auto`.
- Config flow major version `1` until a migration requires changing it.

`tests/test_persistence_contracts.py` guards the stored keys, defaults, profile
fallback, and active platforms. A change to a persisted contract requires an
explicit migration plan and migration tests; changing only the UI label does
not require renaming a stored key.

## Validation Standards

The repository uses four CI boundaries:

| Boundary | Command or action | Purpose |
| --- | --- | --- |
| Static quality | `python -m ruff check .` | Python style and static checks. |
| Fast behavior | `python -m pytest --ignore=tests/test_runtime_smoke.py` | Protocol, Bluetooth, config flow, coordinator, entity, persistence, and packaging behavior with focused substitutes. |
| Real Home Assistant | `python -m pytest tests/test_runtime_smoke.py` | Imports, inheritance, signatures, entity construction, config flow, and lifecycle against installed Home Assistant APIs. |
| Distribution | `hacs/action@main` and `home-assistant/actions/hassfest@master` | HACS repository and Home Assistant integration metadata validation. |

The real-Home-Assistant matrix installs each tested Home Assistant version
under that release's official package constraints. Physical purifier and BLE
range testing remain device-level release checks and cannot be replaced by the
runtime smoke lane.

## Authoritative Documentation

Use current official documentation before examples from blogs or old custom
integrations. Recently maintained Home Assistant Core integrations are useful
when the developer documentation does not show enough implementation detail.

### Home Assistant Developer Documentation

- [Developer documentation](https://developers.home-assistant.io/)
- [Creating an integration](https://developers.home-assistant.io/docs/creating_component_index/)
- [Integration file structure](https://developers.home-assistant.io/docs/creating_integration_file_structure/)
- [Integration manifest](https://developers.home-assistant.io/docs/creating_integration_manifest/)
- [Config flows and config entries](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/)
- [Data entry flows and form sections](https://developers.home-assistant.io/docs/data_entry_flow_index/)
- [Fetching data and `DataUpdateCoordinator`](https://developers.home-assistant.io/docs/integration_fetching_data/)
- [Entity development and naming](https://developers.home-assistant.io/docs/core/entity/)
- [Bluetooth APIs](https://developers.home-assistant.io/docs/core/bluetooth/api/)
- [Integration diagnostics](https://developers.home-assistant.io/docs/core/integration_diagnostics/)
- [Backend localization](https://developers.home-assistant.io/docs/internationalization/core/)
- [Integration Quality Scale](https://developers.home-assistant.io/docs/core/integration-quality-scale/)
- [Development testing](https://developers.home-assistant.io/docs/development_testing/)
- [Home Assistant Core integrations](https://github.com/home-assistant/core/tree/dev/homeassistant/components)
- [Example custom integrations](https://github.com/home-assistant/example-custom-config/tree/master/custom_components)

### Home Assistant User Documentation

- [Devices and services](https://www.home-assistant.io/integrations/)
- [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/)
- [Home Assistant configuration directory](https://www.home-assistant.io/docs/configuration/)
- [Open the Integrations page](https://my.home-assistant.io/redirect/integrations/)

### HACS Documentation

- [Publishing overview and `hacs.json`](https://www.hacs.xyz/docs/publish/start/)
- [Integration repository requirements](https://www.hacs.xyz/docs/publish/integration/)
- [HACS GitHub Action](https://www.hacs.xyz/docs/publish/action/)
- [Default repository inclusion requirements](https://www.hacs.xyz/docs/publish/include/)
- [Adding a custom repository](https://www.hacs.xyz/docs/faq/custom_repositories/)
- [Installing and configuring HACS integrations](https://www.hacs.xyz/docs/use/repositories/type/integration/)
- [Reference integration template](https://github.com/custom-components/blueprint)
