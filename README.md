# Govee BLE Air Purifier

This integration exists first and foremost to expose the purifier's PM2.5 sensor in Home Assistant.

It also gives you basic control of the purifier and shows remaining filter life.

The protocol, profile, framing, and encrypted-session implementation is also a
Home Assistant-independent typed package built from the same HACS-shipped
source. See the [protocol library documentation](docs/protocol-library.md) for
its public API and packaging boundary.

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
physically validated through this integration. H7129 manual-mode and
night-light physical pushes are decoded from its matching decrypted profile
layout but remain physically unverified. Other recognized `H712*` models
use the H7124 protocol fallback and may fail or expose unsupported or mismatched
features.

Model profiles record these evidence levels explicitly: H7124 is `verified`,
H7129 is `read_verified`, and an unbundled H712-family model is `fallback`.
Home Assistant asks for acknowledgement before continuing setup with any profile
that is not fully verified, and diagnostics report the resolved status.

## What You Get

- PM2.5 reading
- Filter life percentage
- Air purifier power control
- Fan speed and mode control
- Optional RGB and brightness night-light control on H7124 and H7129
- Optional integration-managed Custom Auto fan control
- Immediate physical power, fan-mode, and night-light power/brightness updates
  while a push-enabled purifier has a retained BLE connection

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

Home Assistant automatically offers connectable purifiers advertising the
physically observed `GVH712…` or `ihoment_H712…` name formats. Select
**Configure**, confirm the discovered purifier, review its support level, and
choose its polling and Custom Auto options. Discovery never creates an entry
without that confirmation.

You can also go to **Settings > Devices & services**, choose **Add
Integration**, search for **Govee BLE Air Purifier**, and choose a purifier from
the recently seen list or enter its Bluetooth address manually.

Before either setup path creates an entry, the integration opens a temporary
GATT connection and validates the selected profile with read-only state and
status requests. The check has a 65-second operation deadline and a separate
10-second foreground cleanup deadline; cleanup that exceeds that window stays
tracked in the background. It never sends power, fan, or light control
commands. A failed check shows a translated reason and can be retried.

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
H7124 performs one best-effort light reconciliation at startup and then every
five minutes. Its power/brightness and RGB queries are response-paced instead
of written back-to-back, and incomplete reads automatically back off as far as
30 minutes without failing the core purifier poll. Physical pushes and
confirmed Home Assistant commands update the cached state between those reads.
H7129 retains its existing back-to-back encrypted light queries on every
three-second poll.

## Custom Auto

When the resolved model profile provides all required fan modes, setup and the
device's integration Options contain four expanded PM2.5 boundary groups for
five air-quality levels: Excellent, Good, Fair, Bad, and Poor. Each group
contains one shared increase/return boundary and the return delay. The increase
confirmation delay appears above those groups and defaults to 3 seconds. A
positive delay requires two valid PM2.5 readings that both call
for a higher speed; the second reading determines the target. After the first
upward reading, Custom Auto schedules one full-state confirmation poll after
the configured delay unless a newer valid reading arrives first and can confirm
sooner. A failed or PM-invalid dedicated poll preserves the first reading and
leaves confirmation to the next normal poll; it is not retried. Setting the
confirmation delay to 0 disables the second-reading requirement and increases
speed from the first valid reading. Initial detection still follows the
configured polling interval. Turn on the device's `Custom Auto` switch to
activate those rules. Turning the switch off keeps the purifier on and hands
control to its built-in Auto mode. Profiles with narrower fan-mode sets do not
expose these controls.

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
powering on the purifier. This intent survives Home Assistant restarts. The
verified poll responses alone do not report fan mode, but push-enabled H7124 and
H7129 profiles detect physical mode selection while connected. That physical
selection turns off Custom Auto and replaces remembered automatic ownership.
H7129 manual-mode push support is inferred and remains awaiting physical
verification.

H7124 and fallback profiles use boundaries 3, 5, 9, and 15. H7129 uses 7, 9,
13, and 19. At each boundary, an upshift occurs above the value and a delayed
downshift qualifies at or below it. Every boundary and return delay is
independently configurable.
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
  poll or command begins its normal queue and response budgets. A Bluetooth
  backend operation that ignores cancellation is quarantined on its old
  connection and observed in the background; it cannot permanently prevent a
  later poll from creating a fresh connection.
- H7124 keeps its proven power and status queries every 10 seconds and adds
  response-paced, best-effort light reconciliation at startup and every five
  minutes. Missing light telemetry backs off without making the purifier
  unavailable. H7129 retains its reliable encrypted light queries on every
  three-second poll.
- Each purifier retains its own reusable Bluetooth connection by default for
  faster controls and push updates. Push-enabled dedicated H7124 and H7129
  connections remain subscribed until failure, reload, or shutdown, even with
  a long polling interval. If the adapter or proxy has too few GATT connection slots,
  turn on `Share a Bluetooth connection slot` in the Options of at least two
  purifiers. Only opted-in entries share one connection lane; for example, two
  dedicated entries plus two shared entries occupy three slots. Shared controls
  enter the priority scheduler before advertisement recovery and BLE connection
  setup, bypassing routine polls that have not started active BLE work. One poll
  is allowed after every three priority commands so background state cannot
  starve. A shared purifier receives physical pushes only while it owns the
  shared connection, so changes made while another shared purifier owns the
  slot are reconciled by the next poll instead of arriving immediately.
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
stages. Diagnostics also report listener state, reconnect generation, recognized
push counts by type, ignored profile-mismatched pushes, last-push age, and
night-light reconciliation attempts, outcomes, backoff, and last-success age. The
integration's stage messages do not include BLE addresses, packet
payloads, or encryption keys. They include a stable short hashed device label
so same-model purifiers can be distinguished without exposing a full address.
Disconnect and release messages include only that label plus connection and
encrypted-session ages; supporting Home Assistant Bluetooth libraries may
include device identifiers.

An interrupted state poll is retried once after a fresh advertisement and new
encrypted session. Commands are never replayed automatically because the
purifier may have applied a write before its connection dropped.
