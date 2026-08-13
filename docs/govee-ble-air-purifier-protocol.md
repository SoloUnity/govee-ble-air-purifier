# Govee BLE Air Purifier Protocol

## Scope

This document is a command reference for the locally observed Govee `H712*`
BLE air purifier protocol. It currently covers the GoveeLife Smart Air Purifier
2 (`H7124`) and Govee `H7129`. It is based on physical device captures and is
not an official Govee specification.

It focuses on explicit, known commands that can be sent locally over BLE.
Discovery notes, failed probes, and partial sweeps are summarized only where
they affect how commands should be used. Commands are shown as decrypted
20-byte application frames. H7124 sends those frames directly; H7129 encrypts
them with a connection-specific session key before writing them to GATT.

Physical targets and evidence:

| Model | Evidence | BLE name | Firmware | Hardware |
| --- | --- | --- | --- | --- |
| `H7124` | Physically replayed and validated | `GVH712438FE` | `1.00.33` | `4.01.00` |
| `H7129` | App traffic captured and decrypted, including night-light control; integration connection, handshake, polling, disconnect, and recovery physically observed | `ihoment_H7129_*` | Not captured | Not captured |

The H7124 target used macOS CoreBluetooth UUID
`47663FD1-1875-BFAD-C898-D79C0B8F0A3D`. A secondary H7124 unit was observed as
`GVH7124178E`. The H7129 capture covered connection setup, power on, Low,
Medium, High, Sleep, Auto Default, Turbo, status polling, and power off.
An additional H7124 capture from `GVH712438FE` covered night-light power,
brightness, RGB control, and state queries. A further H7129 capture from
`ihoment_H7129_6B51` (SHA-256
`9fb7a73cebee327dd0290473aea45d7caa7317a389134379552a7308cc83a177`) covered
night-light queries, power, brightness, and RGB control; every post-handshake
frame decrypted to a checksum-valid plaintext frame.

## Device And GATT

| Item | UUID |
|------|------|
| Service | `00010203-0405-0607-0809-0a0b0c0d1910` |
| Notify/read characteristic | `00010203-0405-0607-0809-0a0b0c0d2b10` |
| Write/read characteristic | `00010203-0405-0607-0809-0a0b0c0d2b11` |

Usage notes:

- Write 20-byte wire frames to `...2b11` using ATT Write Command without a
  response.
- Subscribe to notifications on `...2b10` to receive responses and pushes.
- Passive notifications usually produce no data until a command is written.
- H7124 wire frames are plaintext. Complete the H7129 encrypted-session
  handshake before sending commands, then encrypt ordinary commands and decrypt
  ordinary notifications with that connection's session key. Delayed handshake
  notifications can still arrive under the communication key during the
  transition to application traffic.
- On macOS, connect with a service filter for `00010203-0405-0607-0809-0a0b0c0d1910` to avoid CoreBluetooth descriptor discovery failures (`CBErrorDomain Code=8`).
- macOS device UUIDs can change between sessions; rescan if a saved UUID stops connecting.
- Only one BLE central can connect to the purifier at a time.

## Frame Format

All observed plaintext application frames are exactly 20 bytes.

```text
Bytes 0-1:   Command/response prefix
Bytes 2-18:  Payload, zero-padded unless command-specific
Byte 19:     XOR checksum of bytes 0-18
```

Prefix families:

| Prefix | Meaning | Safety |
|--------|---------|--------|
| `aa xx` | Query/response commands | Generally read-only |
| `ee xx` | Push notifications from device | Receive-only |
| `3a xx` | Official app-captured control commands | State-changing |
| `33 xx` | Mixed control commands; `33 01` is official app power control | State-changing/unknown |

Checksum example for Low mode:

```text
3a ^ 05 ^ 01 ^ 01 = 3f
```

So the full Low frame is:

```text
3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f
```

The checksum is calculated before H7129 encryption and checked after H7129
decryption. It does not validate encrypted wire bytes directly.

## Model Transport

| Model | Application frames on the wire | Connection setup |
| --- | --- | --- |
| `H7124` | Plaintext | No encrypted-session handshake observed |
| `H7129` | Govee V1 encrypted | Two-step `e7 01` / `e7 02` handshake for every BLE connection |

### H7129 Govee V1 Session

H7129 uses the 16-byte communication key `MakingLifeSmarte`, which is shipped
in the official Govee app, to negotiate a new 16-byte session key:

1. The client builds a checksum-valid `e7 01` frame with random padding,
   encrypts it with the communication key, and writes it to the command
   characteristic.
2. The device sends an encrypted `e7 01` response. After decryption, plaintext
   bytes 2 through 17 contain the connection's session key.
3. The client builds a checksum-valid `e7 02` frame with random padding,
   encrypts it with the communication key, and writes it.
4. The device echoes that `e7 02` frame under the communication key to confirm
   the session.

Only after the confirmation should ordinary commands and notifications use the
session key. The session key belongs to that BLE connection and must be
discarded on disconnect, failed negotiation, or connection replacement.

Physical integration logs captured duplicate handler deliveries of a
checksum-valid communication-key `e7 02` notification after handshake
completion, while the client was waiting for the first application response.
The classification does not establish that its payload exactly matched the
preceding confirmation request. During an application transaction, the client
ignores communication-key `e7 01` or `e7 02` notifications and continues waiting
under the unchanged application deadline. The `e7 01` case is defensive; only
`e7 02` was observed. Other decryption failures still invalidate the connection.
If only stale handshake traffic arrives, the normal response timeout still ends
the operation.

For both the communication key and session key, one 20-byte frame is
transformed as follows:

| Bytes | Transform |
| --- | --- |
| 0-15 | AES-128-ECB, one block, without padding |
| 16-19 | XOR with the first four bytes of an RC4-compatible keystream initialized from the same key |

The RC4-compatible state is initialized for each frame. Decryption applies the
inverse AES operation to bytes 0 through 15 and the same keystream XOR to bytes
16 through 19. Every post-handshake frame in the original app capture decrypted
to a valid XOR-checksummed application frame; the later integration observation
above exposed delayed handshake traffic at the subscription transition.

## Canonical Commands

### 33 01: Power Control

`33 01` is the official app-captured power command on H7124 and H7129. The
plaintext command frames are identical on both models; H7129 encrypts them on
the wire.

Sources: an H7124 iPhone sysdiagnose packet log and the decrypted H7129
PacketLogger session while the official app was toggling purifier power.

In the H7124 log, the app wrote these frames to ATT handle `0x0015`; the device
echoed `33 01` notifications from handle `0x0012`, then reported the applied
state in `aa 01` device-state notifications. Decrypted H7129 `aa 01` responses
use the same power-state layout.

| Action | Command frame | Confirming `aa 01` state |
|--------|---------------|---------------------------|
| Off | `33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32` | Byte 2 = `0x00` |
| On | `33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33` | Byte 2 = `0x01` |

Observed H7124 app-toggle exchanges in that log:

| Time UTC | Direction | ATT op | Handle | Frame | Decoded state |
|----------|-----------|--------|--------|-------|---------------|
| `2026-06-29T12:01:26.032` | App -> purifier | Write Command `0x52` | `0x0015` | `33 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 32` | Off command |
| `2026-06-29T12:01:26.706` | Purifier -> app | Notification `0x1b` | `0x0012` | `aa 01 00 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2a` | Off |
| `2026-06-29T12:01:29.894` | App -> purifier | Write Command `0x52` | `0x0015` | `33 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33` | On command |
| `2026-06-29T12:01:30.636` | Purifier -> app | Notification `0x1b` | `0x0012` | `aa 01 01 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2b` | On |

### 3A 05: Fan Speed / Mode Control

`3a 05` is the official app-captured fan mode command on both models. Prefer
this over all H7124 `33 05` experiments.

Sources: an H7124 iPhone sysdiagnose packet log and the decrypted H7129
PacketLogger session.

The official app wrote the commands to the write characteristic. Both devices
confirmed fan commands with matching plaintext echoes; H7129's wire values
were encrypted.

| Mode | Model | Plaintext command frame |
| --- | --- | --- |
| Low | Both | `3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f` |
| Medium | Both | `3a 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3c` |
| High | Both | `3a 05 01 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3d` |
| Sleep | Both | `3a 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3a` |
| Auto Default | `H7124` | `3a 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 28` |
| Auto Default | `H7129` | `3a 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 2e` |
| Turbo | Both | `3a 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 38` |

Byte map:

| Byte | Field | Values |
|------|-------|--------|
| 0 | Prefix high | `0x3a` |
| 1 | Prefix low | `0x05` |
| 2 | Mode group / special mode ID | `0x01` manual fan level, `0x03` Auto, `0x05` Sleep, `0x07` Turbo |
| 3 | Manual fan level | `0x01` Low, `0x02` Medium, `0x03` High, `0x00` for non-manual modes |
| 4 | Unknown | `0x00` in captured mode writes |
| 5 | Auto parameter | `0x14` for H7124 Auto Default, `0x12` for H7129 Auto Default, otherwise `0x00` |
| 6-18 | Padding/unknown | `0x00` in captured mode writes |
| 19 | XOR checksum | XOR of bytes 0-18 |

Validation:

- The official app captured these exact plaintext frames while cycling Low,
  Medium, High, Sleep, Auto Default, and Turbo on both models.
- Sending the H7124 sequence over BLE successfully cycled the physical
  purifier through `Low -> Medium -> High -> Sleep -> Auto -> Turbo -> Low`.
- Every H7129 command decrypted with a valid checksum, and its command echoes
  decrypted to the expected plaintext.
- Low, Medium, and High did not produce `ee 05` push notifications in either
  capture.

H7129 also offers Quiet and High Efficiency Auto behaviors in addition to
Default. Their command parameters have not yet been captured. The known
`0x12` frame above specifically represents H7129 Auto Default. H7124 Auto
Default uses `0x14`; the differing value is model-specific Auto configuration,
not a different mode ID.

H7124 plaintext CLI example:

```bash
.venv/bin/govee-h7124-ble query --address 47663FD1-1875-BFAD-C898-D79C0B8F0A3D --command "3a 05 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 3f" --seconds 8 --out captures/set-low.jsonl
```

### AA 1B / 3A 1B: Night Light

These frames were captured on both H7124 and H7129. The H7124 source is a
PacketLogger session from `GVH712438FE` on 2026-07-28 while the official app
queried the night-light state, reasserted power on, changed brightness from
100 to 50 to 1 percent, issued green and then blue RGB control writes after
querying a red state, and turned the light off. The H7129 source is a
PacketLogger session from `ihoment_H7129_6B51` (public address
`5C:E7:53:F9:6B:51`) on 2026-07-28 while the official app queried
power/brightness and color state, reasserted light power on, set brightness to 100,
50, 1, and 100 percent, wrote red, yellow, green, blue, and red RGB controls,
and turned the light off. After the Govee V1 handshake, every H7129 frame
decrypted to a checksum-valid plaintext frame, and the decrypted H7129 control
layouts were identical to H7124. The captures prove the commands, device
notifications, and their order; HCI traffic does not by itself verify physical
light output.

Implementation note: the integration intentionally does not replay the H7124
`aa 1b` state queries during routine polling. Adding those reads to every poll
made the previously reliable H7124 core path unreliable. H7124 light controls
remain available and their matching notifications update cached state. H7129's
automatic encrypted night-light polling remains enabled.

H7124 writes used ATT Write Command `0x52` on handle `0x0015`, and
notifications used ATT Notification `0x1b` on handle `0x0012`. H7129 used the
same GATT service and characteristic UUIDs, with notify value handle `0x0016`
and write value handle `0x0019` (HCI connection handle `0x005b`); its writes
also used ATT `0x52` and its notifications ATT `0x1b`, with all frames
encrypted on the wire. All frames below are decrypted plaintext.

#### Power And Brightness

Observed state query on both models:

```text
aa 1b 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b0
```

The response layout is `aa 1b 01 PP BB`, followed by zero bytes through byte 18
and the checksum. `PP` is the reported power flag and `BB` is the reported
brightness. Control notifications use the same state layout with prefix `3a`
instead of `aa`. H7124's startup response reported on at 100 percent; H7129's
startup response reported on at 50 percent:

```text
aa 1b 01 01 32 00 00 00 00 00 00 00 00 00 00 00 00 00 00 83
```

| Action | Model evidence | Control frame | Resulting `3a 1b` state notification |
| --- | --- | --- | --- |
| Power on | Both | `3a 1b 01 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 20` | H7124: `3a 1b 01 01 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 45`; H7129: `3a 1b 01 01 32 00 00 00 00 00 00 00 00 00 00 00 00 00 00 13` |
| Brightness 100% | H7129 | `3a 1b 01 02 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 46` | `3a 1b 01 01 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 45` |
| Brightness 50% | Both | `3a 1b 01 02 32 00 00 00 00 00 00 00 00 00 00 00 00 00 00 10` | `3a 1b 01 01 32 00 00 00 00 00 00 00 00 00 00 00 00 00 00 13` |
| Brightness 1% | Both | `3a 1b 01 02 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 23` | `3a 1b 01 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 20` |
| Power off | Both | `3a 1b 01 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 21` | H7124: `3a 1b 01 00 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 21`; H7129: `3a 1b 01 00 64 00 00 00 00 00 00 00 00 00 00 00 00 00 00 44` |

Control byte map:

| Byte | Field | Observed values |
| --- | --- | --- |
| 0 | Prefix | `0x3a` |
| 1 | Night-light command | `0x1b` |
| 2 | Power/brightness selector | `0x01` |
| 3 | Operation | `0x01` power, `0x02` brightness |
| 4 | Operation value | Power `0x00` off or `0x01` on; brightness `0x01` 1%, `0x32` 50%, `0x64` 100% (100% write observed only on H7129) |
| 5-18 | Unobserved payload | `0x00` in every captured frame |
| 19 | XOR checksum | XOR of bytes 0-18 |

Power/brightness state byte map:

| Byte | Field | Observed values |
| --- | --- | --- |
| 0 | Frame prefix | `0xaa` after a query, `0x3a` after a control write |
| 1 | Night-light command | `0x1b` |
| 2 | Power/brightness selector | `0x01` |
| 3 | Reported power | `0x00` off, `0x01` on |
| 4 | Reported brightness | `0x01` 1%, `0x32` 50%, `0x64` 100% |
| 5-18 | Unobserved payload | `0x00` in every captured frame |
| 19 | XOR checksum | XOR of bytes 0-18 |

The power write only contains the power value; its notification reports the
retained brightness. On H7124 the light already reported on at 100 percent
before the captured power-on write, and turning the light off retained and
reported the brightness value `0x01`. On H7129 the light reported on at 50
percent at capture start, the power-on notification reported the retained 50
percent, and after the explicit 100-percent brightness writes the power-off
notification reported the retained 100 percent. The explicit 100-percent
brightness write is H7129-only evidence; the H7124 100-percent states came
from queries and notifications, not from an explicit brightness write in that
capture.

#### RGB Color

Observed RGB query on both models:

```text
aa 1b 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b4
```

On H7124 the query response layout is `aa 1b 05 0d RR GG BB`, followed by zero
bytes through byte 18 and the checksum. Observed control writes use the same
payload layout with prefix `3a` on both models. H7129's only observed color
query response carried an unknown discriminator instead:

```text
aa 1b 05 fc 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 48
```

The `0xfc` value is undecoded and is not proof of an initial red state; the
H7124 `0x0d` RGB query response layout must not be assumed for H7129.

| Color | Model | Direction | Frame |
| --- | --- | --- | --- |
| Red | H7124 | Queried state | `aa 1b 05 0d ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 46` |
| Red | H7129 | Control and matching notification | `3a 1b 05 0d ff 00 00 00 00 00 00 00 00 00 00 00 00 00 00 d6` |
| Yellow | H7129 | Control and matching notification | `3a 1b 05 0d ff ff 00 00 00 00 00 00 00 00 00 00 00 00 00 29` |
| Green | Both | Control and matching notification | `3a 1b 05 0d 00 ff 00 00 00 00 00 00 00 00 00 00 00 00 00 d6` |
| Blue | Both | Control and matching notification | `3a 1b 05 0d 00 00 ff 00 00 00 00 00 00 00 00 00 00 00 00 d6` |
| Blue | H7124 | Queried state after power off | `aa 1b 05 0d 00 00 ff 00 00 00 00 00 00 00 00 00 00 00 00 46` |

RGB byte map:

| Byte | Field | Observed values |
| --- | --- | --- |
| 0 | Frame prefix | `0xaa` for query responses, `0x3a` for controls and their notifications |
| 1 | Night-light command | `0x1b` |
| 2 | RGB selector | `0x05` |
| 3 | RGB discriminator/subcommand | `0x0d`; its independent meaning is unknown |
| 4 | Red component | `0x00` or `0xff` |
| 5 | Green component | `0x00` or `0xff` |
| 6 | Blue component | `0x00` or `0xff` |
| 7-18 | Unobserved payload | `0x00` in every captured frame |
| 19 | XOR checksum | XOR of bytes 0-18 |

On H7124, the green and blue notifications were byte-for-byte matches of the
writes. Blue was also independently returned by the later RGB query. Green was
not queried before the blue write, so its matching notification should be
treated as an echo-style acknowledgement rather than independent proof of the
applied color. Red was reported by three explicit H7124 RGB queries but was
not set by a control write in that capture. On H7129, every color notification
was an exact echo of the control write in both decrypted plaintext and
encrypted wire bytes, and no color was independently queried afterward, so all
H7129 color notifications are echo-style acknowledgements rather than
independent confirmation of the applied color.

#### Captured Sequence

Times are the query or control write timestamps; each result came from the
following device notification.

H7124 (`GVH712438FE`):

| Time UTC | Operation | Result |
| --- | --- | --- |
| `2026-07-28T14:54:33.665` | Query power/brightness | On, 100% |
| `2026-07-28T14:54:33.725` | Query RGB | Red `(255, 0, 0)` |
| `2026-07-28T14:54:34.625` | Query power/brightness | On, 100% |
| `2026-07-28T14:54:34.685` | Query RGB | Red `(255, 0, 0)` |
| `2026-07-28T14:54:35.674` | Write power on | On, retained 100% |
| `2026-07-28T14:54:36.635` | Query power/brightness | On, 100% |
| `2026-07-28T14:54:36.696` | Query RGB | Red `(255, 0, 0)` |
| `2026-07-28T14:54:41.020` | Write brightness `0x32` | On, 50% |
| `2026-07-28T14:54:44.035` | Write brightness `0x01` | On, 1% |
| `2026-07-28T14:54:50.940` | Write RGB `(0, 255, 0)` | Matching green notification |
| `2026-07-28T14:54:54.121` | Write RGB `(0, 0, 255)` | Matching blue notification |
| `2026-07-28T14:55:00.106` | Write power off | Off, retained brightness 1% |
| `2026-07-28T14:55:00.906` | Query power/brightness | Off, 1% |
| `2026-07-28T14:55:00.996` | Query RGB | Blue `(0, 0, 255)` |

H7129 (`ihoment_H7129_6B51`):

| Time UTC | Operation | Result |
| --- | --- | --- |
| `2026-07-28T16:07:47.533` | Query power/brightness | On, 50% |
| `2026-07-28T16:07:47.593` | Query color state | Unknown `0xfc` response |
| `2026-07-28T16:07:48.402` | Write power on | On, retained 50% |
| `2026-07-28T16:07:50.805` | Write brightness `0x64` | On, 100% |
| `2026-07-28T16:07:54.036` | Write brightness `0x32` | On, 50% |
| `2026-07-28T16:07:55.855` | Write brightness `0x01` | On, 1% |
| `2026-07-28T16:07:57.304` | Write brightness `0x64` | On, 100% |
| `2026-07-28T16:08:00.674` | Write RGB `(255, 0, 0)` | Exact echo notification |
| `2026-07-28T16:08:04.340` | Write RGB `(255, 255, 0)` | Exact echo notification |
| `2026-07-28T16:08:05.706` | Write RGB `(0, 255, 0)` | Exact echo notification |
| `2026-07-28T16:08:07.757` | Write RGB `(0, 0, 255)` | Exact echo notification |
| `2026-07-28T16:08:08.540` | Write RGB `(255, 0, 0)` | Exact echo notification |
| `2026-07-28T16:08:13.241` | Write power off | Off, retained 100% |

Every listed command and notification is a checksum-valid 20-byte frame;
H7129 frames are decrypted plaintext. Only brightness values 1, 50, and 100
and the red, yellow, green, and blue colors were observed; broader accepted
ranges remain unverified. General purifier `aa 01` polling also appeared in
the H7129 capture and is purifier state, not night-light state: the purifier
remained on after the night light was turned off.

### AA 19: Device Status / PM2.5 / Filter Life

The plaintext query is identical on H7124 and H7129:

```text
aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3
```

Typical response:

```text
aa 19 81 00 01 01 00 64 00 00 00 00 00 00 00 00 00 00 00 56
```

Sources: an H7124 iPhone sysdiagnose packet log and decrypted H7129
PacketLogger frames while the official app was polling air quality.

In the H7124 log, the app wrote the query frame to ATT handle `0x0015`, which
maps to characteristic `00010203-0405-0607-0809-0a0b0c0d2b11`. The purifier
returned the status frame as an ATT notification from handle `0x0012`, which
maps to characteristic `00010203-0405-0607-0809-0a0b0c0d2b10`. H7129 uses the
same characteristics and response layout after decryption.

Observed H7124 app-poll exchanges in that log:

| Time UTC | Direction | ATT op | Handle | Frame | Decoded PM2.5 |
|----------|-----------|--------|--------|-------|--------------:|
| `2026-06-29T10:44:00.268` | App -> purifier | Write Command `0x52` | `0x0015` | `aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3` | Query |
| `2026-06-29T10:44:00.327` | Purifier -> app | Notification `0x1b` | `0x0012` | `aa 19 81 00 01 01 00 64 00 00 00 00 00 00 00 00 00 00 00 56` | `1` |
| `2026-06-29T11:37:57.517` | App -> purifier | Write Command `0x52` | `0x0015` | `aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3` | Query |
| `2026-06-29T11:37:57.576` | Purifier -> app | Notification `0x1b` | `0x0012` | `aa 19 81 00 01 01 00 64 00 00 00 00 00 00 00 00 00 00 00 56` | `1` |
| `2026-06-29T11:38:05.796` | App -> purifier | Write Command `0x52` | `0x0015` | `aa 19 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 b3` | Query |
| `2026-06-29T11:38:05.855` | Purifier -> app | Notification `0x1b` | `0x0012` | `aa 19 81 00 01 01 00 64 00 00 00 00 00 00 00 00 00 00 00 56` | `1` |

Complete decrypted H7129 responses reported PM2.5 values of 5, 2, and 4
micrograms per cubic meter and filter life of 99 percent. Each response began
with `aa 19 81` and passed the same plaintext checksum validation.

Byte map:

| Byte | Field | Values / decoding |
|------|-------|-------------------|
| 0 | Prefix high | `0xaa` |
| 1 | Prefix low | `0x19` |
| 2 | Flags/status | Usually `0x81` in captures |
| 3 | PM2.5 / sentinel high byte | Big-endian raw status high byte |
| 4 | PM2.5 / sentinel low byte | Big-endian raw status low byte |
| 5 | Mode-ish field | H7124 observations include `0x01` Low and `0x04` Turbo; not reliable for all control paths |
| 6 | Unknown | `0x00` in captures |
| 7 | Filter life percent | `0x64` = 100% |
| 8-18 | Unknown/padding | `0x00` in captures |
| 19 | XOR checksum | XOR of bytes 0-18 |

PM2.5 decoding:

```text
raw_pm25 = (byte[3] << 8) | byte[4]
pm25_ug_m3 = raw_pm25 when raw_pm25 <= 999
pm25_ug_m3 = unknown/invalid when raw_pm25 > 999
```

Verified readings:

| Bytes 3-4 | PM2.5 | Context |
|-----------|------:|---------|
| `00 01` | 1 | Clean baseline |
| `00 02` | 2 | Clean fluctuation |
| `01 18` | 280 | Fire event decay |
| `02 80` | 640 | Fire event decay |
| `02 fd` | 765 | Fire event decay |
| `03 6c` | 876 | Fire event rising |
| `03 82` | 898 | Fire event peak |

Notes:

- PM2.5 is a sensor reading in ug/m3, not a category value.
- The valid observed/displayed range is `0-999` ug/m3. Values above `999`, especially `ff ff` / `65535`, should be treated as invalid, unavailable, or over-range sentinels rather than published as PM2.5.
- Byte 3 can look like a coarse AQI bucket because it is the high byte of PM2.5.
- The 2026-06-29 11:39 H7124 iPhone app capture confirms the command path and
  field location at PM2.5 `1`; the high-value H7124 fire-event captures confirm
  bytes 3-4 are a big-endian `u16`, not a single byte. Low-value H7129 responses
  are consistent with that decoding.
- `aa19` byte 5 should not be treated as authoritative mode state after every control path.

H7124 plaintext CLI example:

```bash
.venv/bin/govee-h7124-ble status --address 47663FD1-1875-BFAD-C898-D79C0B8F0A3D --json
```

### 33 18: Status Push Request

This command has only been observed on H7124.

Command:

```text
33 18 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 2a
```

Effect:

- Triggers an `ee 19` status push.
- Also observed with an `aa 01` state response during sweeps.

Use `aa 19` for normal status polling. Use `33 18` only when specifically testing push-style status behavior.

## Push Notifications

### EE 05: Mode Change Push

After any required H7129 decryption, `ee 05` appears for Sleep, Auto, and Turbo
mode changes on both models.

Examples:

```text
ee 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ee   Sleep, both
ee 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 fc   H7124 Auto Default
ee 05 03 00 00 12 00 00 00 00 00 00 00 00 00 00 00 00 00 fa   H7129 Auto Default
ee 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ec   Turbo, both
```

Byte map:

| Byte | Field | Values |
|------|-------|--------|
| 0 | Prefix high | `0xee` |
| 1 | Prefix low | `0x05` |
| 2 | Mode ID | `0x03` Auto, `0x05` Sleep, `0x07` Turbo |
| 3-4 | Unknown | `0x00` in captures |
| 5 | Mode parameter | `0x14` for H7124 Auto Default, `0x12` for H7129 Auto Default, otherwise `0x00` in known captures |
| 6-18 | Unknown/padding | `0x00` in captures |
| 19 | XOR checksum | XOR of bytes 0-18 |

Low, Medium, and High do not generate `ee 05` pushes in current captures. The
H7129 Quiet and High Efficiency Auto parameters remain uncaptured.

### EE 19: Status Push

Observed on H7124 after `33 18 01`:

```text
ee 19 81 00 01 01 00 64 00 00 00 00 00 00 00 00 00 00 00 12
```

Payload bytes 2-18 appear to match the `aa 19` status format.

### EE AA: Connection / Heartbeat Push

Observed H7124 frame:

```text
ee aa 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 44
```

H7129 emitted the same checksum-valid plaintext in the night-light capture,
encrypted under the communication key after notification subscription and
before the `e7 01` handshake request.

Likely connection handshake, heartbeat, or keepalive. All payload bytes are zero in current captures.

## Query Commands

### AA 01: Device State

The plaintext query is identical on H7124 and H7129:

```text
aa 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ab
```

Observed H7124 response while powered on:

```text
aa 01 01 00 81 00 01 01 00 00 00 00 00 00 00 00 00 00 00 2b
```

Known fields:

| Byte | Field | Values |
|------|-------|--------|
| 2 | Power state | `0x01` observed while on |
| 4 | Flags/status | `0x81`, matching `aa19` byte 2 |
| 6-7 | Unknown | `0x01 0x01` in captures |

Decrypted H7129 responses use the same `aa 01` marker, byte 2 power state, and
`0x81` value in byte 4.

### AA 06 / AA 21: Firmware Version

These queries have only been observed on H7124.

Queries:

```text
aa 06 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ac
aa 21 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8b
```

Both return firmware version text in current captures:

```text
1.00.33
```

### AA 07 / AA 20: Hardware Version / Device Data

These queries have only been observed on H7124.

Known query examples:

```text
aa 07 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ae
aa 20 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 8a
```

Observed hardware version text:

```text
4.01.00
```

`aa 07` also returned device-specific binary blobs for subcommands in app captures, including values related to `ac 27 6e 0c 38 fe` / `ac 27 6e 0c 38 fc`.

### AA 05: Mode-Related Query

This query has only been observed on H7124.

Observed responses include:

```text
aa 05 00 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 aa
aa 05 01 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 ac
aa 05 03 00 00 14 00 00 00 00 00 00 00 00 00 00 00 00 00 b8
```

This appears mode-related and mirrors parts of the `3a 05` mode layout, but the query semantics are not fully decoded.

### Other Observed AA Queries

Except for `aa 1b`, whose app observations now include H7129, the app
observations and sweep results in this section are H7124-only evidence.

| Query | Observed meaning / response |
|-------|-----------------------------|
| `aa 14` | Device ID / serial-like binary blob |
| `aa 16` | Unknown, possibly timer/schedule-related |
| `aa 1b` | Night-light power/brightness (`0x01`) and RGB (`0x05`) state queries |
| `aa 1e` | Unknown app query with echoed response |
| `aa ab` | Unknown, returned `aa ab 02 ... 03` in sweep |
| `aa b1` | Device-specific binary blob |
| `aa ef` | Unknown, returned `aa ef 00 01 01 ... 45` |

Full `AA 00-FF` zero-payload sweep found non-empty responses from:

```text
aa01, aa05, aa06, aa07, aa14, aa16, aa19, aa20, aa21, aaab, aab1, aaef
```

## Experimental Commands

The experimental commands and sweeps in this section were performed only on
H7124. They are not the official app-captured fan mode path. Treat them as
experimental unless explicitly listed above as canonical, and do not assume
H7129 behaves the same after decryption.

### 33 05: Alternate Fan / Mode Path

`33 05` can affect mode, but it is not the command shape used by the official Govee app for fan control. Prefer `3a 05`.

Observed frames:

| Command | Observed result |
|---------|-----------------|
| `33 05 07 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 31` | Physically set Turbo / strongest fan speed |
| `33 05 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 33` | Physically set Sleep / quiet mode |
| `33 05 03 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 35` | Set Auto and physically changed out of Turbo |
| `33 05 01 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 37` | App UI changed to Medium, physical fan stayed Turbo |
| `33 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 36` | No visible UI or physical change |
| `33 05 02 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 34` | No visible UI change while already showing Medium |

ACK behavior:

```text
33 05 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 36
```

The ACK zeroes the payload, so it does not reveal the applied mode.

### 33 02: Power / Connection Control Candidate

`33 02` triggered `ee aa` push notifications and `aa 01` state responses.

Important negative result:

```text
33 02 04 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 35
```

This ACKed as `33 02 01 ... 30` but did not physically change fan speed.

### 33 08 / 33 09 / 33 0A

Observed during opcode sweeps:

| Opcode | Observed behavior |
|--------|-------------------|
| `33 08` | ACKed with zero payload and triggered `ee aa` |
| `33 09` | ACKed with zero payload and triggered `ee aa` |
| `33 0a` | Triggered `ee aa` and `aa 01`, similar to `33 02` |

These may change state. Do not use broad sweeps against a normal room purifier unless physically safe.

## Evidence Summary

### Fan Mode Evidence

| Model | Evidence | Result |
| --- | --- | --- |
| `H7124` | iPhone sysdiagnose PacketLogger file | Captured official app `3a 05` writes for all six app modes |
| `H7124` | Manual BLE replay | `3a 05` sequence cycled `Low -> Medium -> High -> Sleep -> Auto -> Turbo -> Low` successfully |
| `H7124` | App mode pushes | Sleep, Auto Default, and Turbo captured as `ee 05` pushes |
| `H7124` | Earlier `33 05` probes | Showed an alternate/non-app path with partial physical effects |
| `H7129` | Decrypted official-app PacketLogger session | Captured Low, Medium, High, Sleep, Auto Default, and Turbo commands with valid checksums and matching command echoes |
| `H7129` | Decrypted app mode pushes | Sleep, Auto Default, and Turbo captured as `ee 05` pushes |

### PM2.5 Evidence

| Model | Capture | Result |
| --- | --- | --- |
| `H7124` | `captures/diff-aa19-baseline.jsonl` | 30 samples at PM2.5 `1` |
| `H7124` | `captures/diff-aa19-fire-now.jsonl` | 60 samples ranging `541-898` |
| `H7124` | `captures/verify-pm25-now.jsonl` | PM2.5 `280` |
| `H7124` | `captures/current-pm25-check.jsonl` | 5 samples at PM2.5 `1` |
| `H7129` | Decrypted official-app PacketLogger session | Complete responses reported PM2.5 `5`, `2`, and `4`, with filter life at 99% |

The H7124 fire-event series changed smoothly across `aa19` bytes 3-4 and
matched the app-reported `720+ ug/m3` range, confirming raw PM2.5 decoding. The
H7129 values are consistent with the same response layout.

### Night-Light Evidence

| Model | Evidence | Result |
| --- | --- | --- |
| `H7124` | Official-app PacketLogger session | Captured power on/off, brightness 50% and 1%, green and blue RGB writes, matching notifications, and power/brightness and RGB state queries |
| `H7124` | Queried state before and after controls | Reported initial on/100%/red state and final off/1%/blue state |
| `H7129` | Decrypted official-app PacketLogger session | Captured power/brightness and color queries, power on/off, brightness 100%, 50%, and 1% writes, and red, yellow, green, and blue RGB writes, all checksum-valid after decryption |
| `H7129` | Notification behavior | Power/brightness notifications used the normalized state layout with retained brightness; every color notification was an exact echo and no color was independently re-queried |

### H7129 Encryption Evidence

| Evidence | Result |
| --- | --- |
| Official-app connection setup | Identified the communication-key `e7 01` / `e7 02` exchange and a new 16-byte session key |
| Frame transforms | AES-128-ECB correctly decoded bytes 0-15 and an RC4-compatible keystream correctly decoded bytes 16-19 |
| Post-handshake traffic | Every decrypted application frame had a valid XOR checksum |
| Session behavior | Commands and notifications used the negotiated key rather than reusable encrypted constants |
| Integration physical session | Connected, completed the encrypted handshake, polled state, handled disconnect, and recovered with a fresh session |

## Open Questions

| Feature | Status |
|---------|--------|
| H7129 state-changing integration commands | Implemented from decrypted captures; physical command validation through this integration remains pending |
| Night-light integration replay | Profile-backed H7124 and H7129 control is implemented; physical replay through this integration remains pending |
| H7129 `0xfc` color response | Unknown; it is the only observed H7129 color-query response and does not match the H7124 `0x0d` RGB layout |
| Night-light ranges | Power, brightness 1/50/100 percent, and the captured RGB colors are decoded on both models; broader brightness and RGB ranges and other color modes remain unverified |
| Display toggle | Not decoded on H7124; H7129 not investigated |
| Timer/schedule control | Not decoded on H7124; H7129 not investigated |
| Scene selection | Not decoded on H7124; may use a multi-frame protocol; H7129 not investigated |
| Historical chart data | Not found over H7124 BLE; likely app-local history from polling |
| `aa14`, `aa16`, `aab1`, `aaef` | Observed on H7124 but not decoded |
| `ee aa` | Observed on H7124; likely heartbeat/connection event; not fully decoded |

## macOS BLE Notes

- Use the CoreBluetooth UUID reported by scans, not the device MAC address.
- Device UUIDs can change between sessions; rescan before connecting.
- If the iPhone app is connected over BLE, the Mac may fail to connect or receive no notifications.
- Bluetooth permission must be granted to the terminal or host app in System Settings > Privacy & Security > Bluetooth.
- Wi-Fi app changes can still propagate as BLE push notifications while the Mac holds a BLE connection.
