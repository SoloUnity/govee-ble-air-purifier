# Govee BLE Air Purifier

This integration exists first and foremost to expose the purifier's PM2.5 sensor in Home Assistant.

It also gives you basic control of the purifier and shows remaining filter life.

## Supported Devices

- Govee `H712*` family BLE air purifiers
- Devices with Bluetooth names containing `H712` followed by one ASCII letter
  or digit, matched case-insensitively (for example `GVH7124`, `GVH712C`, or
  `ihoment_H7129_6A7D`)

Exact protocol profiles are included for the plaintext Govee H7124 and the
encrypted Govee H7129. The H7124 integration has been physically tested and
validated. H7129 support is implemented and tested against decrypted physical
device captures, but direct control by this integration has not yet been
physically replayed. Other recognized `H712*` models use the H7124 protocol
fallback and may fail or expose unsupported or mismatched features.

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
percentage or Manual preset turns Custom Auto off. Selecting the fan's Auto
preset uses the purifier's built-in Auto mode.

Turning the purifier off suspends either selected Auto mode instead of
forgetting it. A later Home Assistant or physical-button power-on resumes
hardware Auto or Custom Auto, including the last Custom Auto speed. The Custom
Auto switch remains on while that selection is suspended. Turning the switch
off while the purifier is off changes the resume target to hardware Auto without
powering on the purifier. This intent survives Home Assistant restarts; mode
changes made directly on the purifier cannot be detected because its verified
poll responses do not report fan mode.

The defaults reproduce the original Home Assistant automations: Excellent to
Good increases to 40% above 3 and returns to 20% at or below 3 after 7 minutes;
Good to Fair increases to 60% above 5 and returns to 40% at or below 5 after 5
minutes; Fair to Bad increases to 80% above 9 and returns to 60% at or below 9
after 5 minutes; Bad to Poor increases to 100% above 15 and returns to 80% at
or below 15 after 5
minutes. Every threshold and return delay is independently configurable.
Equality at a return boundary still qualifies for a downshift because the rule
uses `<=`; only a valid reading above that boundary resets its timer.

## Notes

- Keep your Home Assistant Bluetooth adapter or Bluetooth proxy close enough to
  the purifier for a reliable connection.
- Use Automatic or Active scanning for reconnectable purifiers. After an
  unexpected disconnect, the integration waits for a fresh connectable
  advertisement before opening a new GATT session; Automatic mode stays
  temporarily Active through the bounded connection attempt without changing
  the saved adapter mode.
- Connection establishment and encrypted-session negotiation have their own
  bounded timeouts. The shorter poll or command timeout starts only after those
  phases complete.
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

## Troubleshooting

From the integration's page in **Settings > Devices & services**, select
**Enable debug logging**, reproduce the problem, and then disable debug logging
to download the log. Debug output identifies connection, encrypted-handshake,
notification, request, response, connection-release, and Bluetooth allocator
stages. The integration's stage messages do not include BLE addresses, packet
payloads, or encryption keys. Disconnect and release messages include only
connection and encrypted-session ages; supporting Home Assistant Bluetooth
libraries may include device identifiers.

An interrupted state poll is retried once after a fresh advertisement and new
encrypted session. Commands are never replayed automatically because the
purifier may have applied a write before its connection dropped.
