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
validated. Physical H7129 integration evidence confirms connection, encrypted
handshake, polling, disconnect, and recovery. State-changing H7129 commands are
implemented from decrypted physical-device captures but have not yet been
physically validated through this integration. Other recognized `H712*` models
use the H7124 protocol fallback and may fail or expose unsupported or mismatched
features.

## What You Get

- PM2.5 reading
- Filter life percentage
- Air purifier power control
- Fan speed and mode control
- Optional RGB and brightness night-light control on H7124 and H7129
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

## Night Light

The exact H7124 and H7129 model profiles expose one Home Assistant `light`
entity with power, brightness, and RGB controls. The capability is declared by
an optional command block in each model profile; fallback profiles without that
block do not create a light entity.

Both models use the same application commands. H7124 sends them as plaintext,
while H7129 uses its connection-specific encrypted session. H7129 accepts the
same RGB state query, but the captured `0xfc` response does not identify a
color. Its color therefore remains unknown after restart until a query returns
an RGB payload or Home Assistant receives an exact echo for a color command.

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
  advertisement before opening a new GATT session. On-demand temporary
  Automatic-to-Active switching requires Home Assistant 2026.6 or newer, is
  best-effort and subject to upstream duration clamping, and is requested only
  while waiting for a new advertisement. Older Home Assistant installations
  may require an explicitly Active scanner when active discovery is needed.
- Fresh-advertisement recovery, connection and service discovery, encrypted
  negotiation, application polling, command confirmation, and disconnect
  cleanup use separate bounded timeouts. Idle cleanup also completes before a
  poll or command begins its normal queue and response budgets.
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
payloads, or encryption keys. They include a stable short hashed device label
so same-model purifiers can be distinguished without exposing a full address.
Disconnect and release messages include only that label plus connection and
encrypted-session ages; supporting Home Assistant Bluetooth libraries may
include device identifiers.

An interrupted state poll is retried once after a fresh advertisement and new
encrypted session. Commands are never replayed automatically because the
purifier may have applied a write before its connection dropped.
