# Govee BLE Air Purifier

This integration exists first and foremost to expose the purifier's PM2.5 sensor in Home Assistant.

It also gives you basic control of the purifier and shows remaining filter life.

## Supported Devices

- Govee `H712*` family BLE air purifiers
- Devices with Bluetooth names starting with `GVH712` followed by one
  alphanumeric model character (for example `GVH7124` for the H7124 or
  `GVH712C` for the H712C)

**Only the Govee H7124 has been physically tested and validated.** Other `H712*`
models are recognized but run on the H7124 protocol definition, so they may
fail to respond or expose unsupported or mismatched features. Recognition of
the family does not mean every `H712*` model is verified.

## What You Get

- PM2.5 reading
- Filter life percentage
- Air purifier power control
- Fan speed and mode control
- Optional integration-managed Custom Auto fan control

## Installation

Requires Home Assistant 2024.8.0 or newer.

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
4. Choose your purifier from the recently seen Bluetooth list if it appears, or
   enter its Bluetooth address manually.

Setup is always this manual flow. The integration does not add itself through
automatic Bluetooth discovery.

## Custom Auto

When the resolved model profile provides all required fan modes, setup and the
device's integration Options contain four expanded PM2.5 boundary groups for
five air-quality levels: Excellent, Good, Fair, Bad, and Poor. Each group
contains an increase threshold, a delayed return threshold, and the return
delay. An increase requires two valid PM2.5 readings that both call for a
higher speed; the second reading determines the target. Turn on the device's
`Custom Auto` switch to activate those rules. Turning the switch off keeps the
purifier on and hands control to its built-in Auto mode. Profiles with narrower
fan-mode sets do not expose these controls.

While Custom Auto is on, the fan's logical preset remains Auto while Home
Assistant sends the underlying manual speeds Sleep (20%), Low (40%), Medium
(60%), High (80%), and Turbo (100%) according to PM2.5. Selecting a manual
percentage or Manual preset, or turning the purifier off, turns Custom Auto off.
Selecting the fan's Auto preset uses the purifier's built-in Auto mode.

The defaults reproduce the original Home Assistant automations: Excellent to
Good increases to 40% above 3 and returns to 20% at or below 3 after 7 minutes;
Good to Fair increases to 60% above 5 and returns to 40% at or below 5 after 5
minutes; Fair to Bad increases to 80% above 9 and returns to 60% at or below 9
after 5 minutes; Bad to Poor increases to 100% above 15 and returns to 80% at
or below 14 after 5
minutes. Every threshold and return delay is independently configurable.
Equality at a return boundary still qualifies for a downshift because the rule
uses `<=`; only a valid reading above that boundary resets its timer.

## Notes

- Keep your Home Assistant Bluetooth adapter or Bluetooth proxy close enough to
  the purifier for a reliable connection.
- The integration adapts healthy-connection reuse to the polling interval. For
  intervals up to 25 seconds, it retains the connection for the interval plus a
  5-second margin, capped at 30 seconds. Longer intervals use a 5-second grace
  for related commands and refreshes before releasing the connection.
- If setup or controls do not respond, close the Govee app and try again. The
  purifier may only allow one Bluetooth connection at a time, so the app may
  also be unable to connect while Home Assistant is actively polling.
- The config entry remembers your exact detected model (for example H7126)
  even while it uses the shared H7124 protocol definition, so a future
  model-specific profile takes effect after an update and a Home Assistant
  restart.
- This integration works locally over Bluetooth.
