"""Compatibility exports for reusable Govee V1 crypto helpers."""

from ..govee_ble_air_purifier_protocol.govee_v1 import (
    COMMUNICATION_KEY,
    HANDSHAKE_PAYLOAD_LENGTH,
    KEY_LENGTH,
    SESSION_KEY_END,
    SESSION_KEY_START,
    build_handshake_request,
    decrypt_frame,
    encrypt_frame,
    identify_handshake_frame,
    parse_session_key,
    validate_handshake_confirmation,
)

__all__ = [
    "COMMUNICATION_KEY",
    "HANDSHAKE_PAYLOAD_LENGTH",
    "KEY_LENGTH",
    "SESSION_KEY_END",
    "SESSION_KEY_START",
    "build_handshake_request",
    "decrypt_frame",
    "encrypt_frame",
    "identify_handshake_frame",
    "parse_session_key",
    "validate_handshake_confirmation",
]
