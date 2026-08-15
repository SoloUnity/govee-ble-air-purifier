# Protocol Fixture Corpus

The files under `tests/fixtures/<model>/protocol.json` are a small, deterministic
regression corpus derived from protocol evidence already documented in this
repository. They are not raw Bluetooth captures and must never become a raw
capture archive.

The fixture loader in `tests/helpers/protocol_fixtures.py` validates every file
before a regression test uses it. It rejects unknown or missing schema fields,
duplicate IDs, invalid 20-byte application frames, checksum failures, mismatched
per-frame SHA-256 digests, unknown provenance references, and unsafe
sanitization attestations.

## What A Fixture Contains

Each model document records:

- The exact model and application protocol.
- Whether application frames travel as plaintext or through Govee V1
  encryption.
- The overall evidence level without overstating physical validation.
- Sanitized provenance references and, when already published in the protocol
  document, the source capture's SHA-256 digest.
- Minimal 20-byte plaintext application frames, their own SHA-256 digests, and
  the expected production decoder interpretation.

H7129 entries intentionally contain only decrypted application frames. Tests
exercise the Govee V1 transform with a clearly synthetic key generated from a
fixed byte sequence. Captured handshake payloads, connection session keys, and
encrypted wire frames are not retained in this corpus.

## Sanitization Rules

Before adding or updating a fixture:

1. Extract only the minimum checksum-valid 20-byte application frame needed to
   reproduce the behavior.
2. Remove Bluetooth addresses, CoreBluetooth UUIDs, local-name suffixes,
   timestamps, connection handles, machine paths, user labels, and unrelated
   traffic.
3. Never include raw capture records, authentication material, communication
   secrets, session keys, handshake random payloads, or user data.
4. Point `reference` at the existing test or protocol-document section that
   supports the bytes and interpretation. Do not use a fixture to introduce an
   undocumented protocol claim.
5. Include `capture_sha256` only when that digest is already safely published in
   the protocol documentation. It identifies the source without embedding it.
6. Compute `plaintext_sha256` over the exact 20 decoded bytes—not the spaced hex
   text—and update it intentionally whenever those bytes change.
7. Choose a narrow `verification_level`. In particular, a decrypted command or
   echo is not evidence that physical device output was observed.

The required sanitization object must remain:

```json
{
  "identifiers_removed": true,
  "timestamps_removed": true,
  "raw_capture_included": false,
  "key_material_included": false
}
```

The loader fails closed if any of those assertions change.

## Adding Evidence

First document the source, decoding, uncertainty, and physical-validation scope
in `docs/govee-ble-air-purifier-protocol.md`. Then add the smallest useful frame
to the matching model fixture with a unique ID, provenance ID, direction, role,
frame digest, and explicit expected interpretation.

Run the focused fixture suite:

```bash
pytest -q tests/test_protocol_fixtures.py
```

Then run the existing framing, encryption, protocol, and profile suites to make
sure the new evidence remains consistent with the broader implementation.
