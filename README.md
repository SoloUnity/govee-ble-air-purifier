# Govee BLE Air Purifier

Home Assistant custom integration for GoveeLife Smart Air Purifier 2 / H7124-style BLE air purifiers.

## Supported Devices

- Model: `H7124`
- BLE local names: `GVH7124*`, such as `GVH712438FE` or `GVH7124178E`

The integration communicates locally over BLE. The Govee app and Home Assistant cannot usually connect to the purifier at the same time, so close the Govee app if setup or polling times out.

## Entities

- `switch`: Power control. Power is intentionally separate from fan speed/mode.
- `select`: Fan mode with options `Low`, `Medium`, `High`, `Sleep`, `Auto`, `Turbo`. There is no `Off` option.
- `sensor`: PM2.5 raw value in `ug/m3`.
- `sensor`: Remaining filter life percent.

Selecting a fan mode while the purifier is off powers it on first, then sends the selected mode. The selector reports the last commanded mode because the observed status frame does not reliably expose authoritative fan mode after all control paths.

## Polling

The coordinator polls every 45 seconds. This is a middle ground between responsive air-quality/filter updates and avoiding excessive active BLE connections for a device that allows only one central connection at a time.

Polling reads power and status in one BLE connection with one notification subscription. Command handling is serialized, and selecting a fan mode while the purifier is off batches power-on and fan-mode writes in one connection where possible.

## Installation

### HACS Custom Repository

1. In Home Assistant, open HACS.
2. Open Custom repositories.
3. Add `https://github.com/SoloUnity/govee-ble-air-purifier` with category `Integration`.
4. Install `Govee BLE Air Purifier` from HACS.
5. Restart Home Assistant.
6. Open Settings > Devices & services.
7. Use Bluetooth discovery for a `GVH7124*` device, or add the integration manually with the BLE address.

### Manual Installation

1. Copy `custom_components/govee_ble_air_purifier` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Open Settings > Devices & services.
4. Use Bluetooth discovery for a `GVH7124*` device, or add the integration manually with the BLE address.

The issue tracker is `https://github.com/SoloUnity/govee-ble-air-purifier/issues`.

## Example Dashboard Card

Replace entity IDs with the names generated in your Home Assistant instance.

```yaml
type: entities
title: Air Purifier
entities:
  - entity: switch.govee_h7124_air_purifier_power
    name: Power
  - entity: select.govee_h7124_air_purifier_fan_mode
    name: Fan mode
  - entity: sensor.govee_h7124_air_purifier_pm2_5
    name: PM2.5
  - entity: sensor.govee_h7124_air_purifier_filter_life
    name: Filter life
```

## Protocol Notes

- GATT service: `00010203-0405-0607-0809-0a0b0c0d1910`
- Notify/read characteristic: `00010203-0405-0607-0809-0a0b0c0d2b10`
- Write/read characteristic: `00010203-0405-0607-0809-0a0b0c0d2b11`
- Frames are exactly 20 bytes.
- Byte 19 is the XOR checksum of bytes 0 through 18.
- PM2.5 is decoded from status response bytes 3 and 4 as a big-endian unsigned integer.
- Filter life percent is status response byte 7.
- The `aa19` status frame is not treated as authoritative for fan mode. The selector reports the last command sent from Home Assistant.

## Extending Model Support

Model-specific BLE behavior lives in `custom_components/govee_ble_air_purifier/profiles.py`. To add another purifier, add a `ModelProfile` with its local-name matcher/prefixes, UUIDs, command frames, fan-mode options, query frames, and decoder functions, then register it in `PROFILES`.

Keep shared BLE orchestration in `client.py`, shared state handling in `coordinator.py`, and entity behavior profile-driven. Add pure tests for profile matching, exact frames, decoder behavior, and coordinator/client behavior before adding production support for a new model.

## Development

Run pure unit tests without installing Home Assistant:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall custom_components tests
```

Full Home Assistant runtime tests are not included because the local development dependencies are intentionally lightweight. Validate BLE behavior with real H7124 hardware before relying on the integration for automation.

The Govee app and Home Assistant may contend for the purifier's single BLE central connection. If setup, polling, or commands time out, close the Govee app and retry from Home Assistant.
