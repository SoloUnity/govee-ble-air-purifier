# Govee BLE Air Purifier

This integration exists first and foremost to expose the purifier's PM2.5 sensor in Home Assistant.

It also gives you basic control of the purifier and shows remaining filter life.

## Supported Device

- Govee H7124-style BLE air purifiers
- Devices with Bluetooth names starting with `GVH7124`

## What You Get

- PM2.5 reading
- Filter life percentage
- Air purifier power control
- Fan speed and mode control

## Installation

### Via HACS

1. Make sure HACS is installed.
2. In Home Assistant, open HACS.
3. Open the three-dot menu in the top-right corner.
4. Choose Custom repositories.
5. Paste `https://github.com/SoloUnity/govee-ble-air-purifier` into the repository field.
6. Choose Integration as the type.
7. Click Add.
8. Search for Govee BLE Air Purifier in HACS.
9. Click Download.
10. Restart Home Assistant.

## Setup

1. Go to Settings > Devices & services.
2. Choose Add Integration.
3. Search for Govee BLE Air Purifier.
4. Choose your purifier if it appears, or enter its Bluetooth address manually.

## Notes

- Keep your Home Assistant Bluetooth adapter or Bluetooth proxy close enough to the purifier for a reliable connection.
- If setup or controls do not respond, close the Govee app and try again. The purifier may only allow one Bluetooth connection at a time.
- This integration works locally over Bluetooth.
