# Govee BLE Air Purifier Protocol Library

`govee-ble-air-purifier-protocol` is the Home Assistant-independent protocol
layer used by this repository's custom integration. It provides validated
20-byte framing, Govee V1 encrypted-session helpers, immutable device-state
models, response decoders, and bundled model profiles for the Govee H712
family.

The same source is shipped inside the HACS custom component and mapped by
`pyproject.toml` to the independently installable top-level package
`govee_ble_air_purifier_protocol`. The integration therefore does not require
an unpublished dependency or download anything at runtime.

## Public API

Import supported symbols from the package root:

```python
from govee_ble_air_purifier_protocol import (
    ProtocolError,
    build_frame,
    decode_status,
    get_profile,
)

profile = get_profile("h7124")
query = profile.status_query_command
frame = build_frame(bytes.fromhex("aa 19 81 00 08 00 00 64"))
status = decode_status(frame)
```

The root API includes:

- `ModelProfile`, profile capability types, `get_profile`, `match_profile`,
  and address/name normalization helpers.
- `DecodedStatus`, `NightLightState`, `PurifierState`, and
  `PurifierPushUpdate`.
- Frame construction and validation plus protocol matchers and decoders.
- Govee V1 handshake, encryption, and decryption helpers.
- `PROFILE_DIRECTORY` and `MODEL_PROFILE_SCHEMA_PATH` for packaged data.

The distribution includes the model JSON definitions, their JSON Schema, and
the `py.typed` marker. Home Assistant transport, discovery, config flows,
coordinators, and entities deliberately remain outside this package.

## Compatibility and publication

Existing imports through
`custom_components.govee_ble_air_purifier.models`, `profiles`, `protocol`,
`bluetooth.framing`, and `bluetooth.govee_v1` remain available as compatibility
facades. New transport-independent consumers should use the top-level package.

The repository can build a wheel with `python -m build --wheel`. Publishing
that wheel is a separate release action and is not required for HACS installs.
