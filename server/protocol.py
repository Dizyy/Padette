"""JoyPad wire protocol, version 1.

Pure validation logic with no dependency on FastAPI, the gamepad driver or the web client
(pad.py only provides the button bits and the PadState type).
See PROTOCOL.md for the human-readable specification.
"""

from __future__ import annotations

import base64
import json
import math
from typing import Any

from .pad import VALID_BUTTONS, PadState

PROTOCOL_VERSION = 1

MAX_PIN_LENGTH = 32
MAX_TOKEN_LENGTH = 64
ENC_NONCE_BYTES = 16


class ProtocolError(Exception):
    """A message was rejected. `code` is a stable, machine-readable string."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def make(type_: str, **fields: Any) -> dict[str, Any]:
    """Build an outgoing (server -> client) message."""
    return {"v": PROTOCOL_VERSION, "type": type_, **fields}


def error(code: str, message: str, ref: str | None = None) -> dict[str, Any]:
    msg = make("error", code=code, message=message)
    if ref is not None:
        msg["ref"] = ref
    return msg


def _number(msg: dict, field: str, limit: float) -> float:
    value = msg.get(field, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtocolError("bad_field", f"'{field}' must be a finite number")
    if abs(value) > limit:
        raise ProtocolError("bad_field", f"'{field}' must be between -{limit} and {limit}")
    return float(value)


def _validate_auth(msg: dict) -> dict:
    # Either the PIN, or the token from an earlier auth_ok (lets the phone log in again
    # after iOS reloaded the page, without asking for the PIN).
    if "token" in msg and "pin" not in msg:
        token = msg["token"]
        if not isinstance(token, str) or not 0 < len(token) <= MAX_TOKEN_LENGTH:
            raise ProtocolError("bad_field", "'token' must be a string")
        return {"token": token}
    pin = msg.get("pin")
    if not isinstance(pin, str) or not 0 < len(pin) <= MAX_PIN_LENGTH:
        raise ProtocolError("bad_field", "'pin' must be a string")
    return {"pin": pin}


def _validate_enc_hello(msg: dict) -> dict:
    # Starts an encrypted connection; "nonce" is the client's random value (base64url, 16 bytes).
    nonce = msg.get("nonce")
    try:
        raw = base64.urlsafe_b64decode(nonce + "=" * (-len(nonce) % 4)) if isinstance(nonce, str) else b""
    except ValueError:
        raw = b""
    if len(raw) != ENC_NONCE_BYTES:
        raise ProtocolError("bad_field", f"'nonce' must be {ENC_NONCE_BYTES} random bytes in base64url")
    return {"nonce": raw}


def _validate_pad(msg: dict) -> dict:
    # The whole controller state, sent on every change and at least every 250 ms.
    # "b": button bitmask (XUSB values, see pad.py); sticks -1..1 with +y = up; triggers 0..1.
    for field in ("b", "lx", "ly", "rx", "ry", "lt", "rt"):
        if field not in msg:
            raise ProtocolError("bad_field", f"'{field}' is missing")
    buttons = msg["b"]
    if isinstance(buttons, bool) or not isinstance(buttons, int) or buttons < 0 or buttons & ~VALID_BUTTONS:
        raise ProtocolError("bad_field", "'b' must be a button bitmask (see PROTOCOL.md)")
    lt, rt = _number(msg, "lt", 1), _number(msg, "rt", 1)
    if lt < 0 or rt < 0:
        raise ProtocolError("bad_field", "'lt' and 'rt' must be between 0 and 1")
    return {"state": PadState(buttons, _number(msg, "lx", 1), _number(msg, "ly", 1),
                              _number(msg, "rx", 1), _number(msg, "ry", 1), lt, rt)}


def _validate_ping(msg: dict) -> dict:
    # "id" is optional and echoed back verbatim in the pong.
    if "id" in msg and not isinstance(msg["id"], (int, float, str)):
        raise ProtocolError("bad_field", "'id' must be a number or string")
    return {"id": msg.get("id")}


_VALIDATORS = {
    "auth": _validate_auth,
    "enc_hello": _validate_enc_hello,
    "ping": _validate_ping,
    "pad": _validate_pad,
}

MESSAGE_TYPES = tuple(_VALIDATORS)


def parse(raw: str) -> tuple[str, dict]:
    """Parse and validate one incoming text frame.

    Returns (type, normalized_fields). Raises ProtocolError on any problem.
    """
    try:
        msg = json.loads(raw)
    except (ValueError, TypeError):
        raise ProtocolError("bad_json", "message is not valid JSON") from None
    if not isinstance(msg, dict):
        raise ProtocolError("bad_json", "message must be a JSON object")
    if msg.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("bad_version", f"'v' must be {PROTOCOL_VERSION}")
    type_ = msg.get("type")
    validator = _VALIDATORS.get(type_) if isinstance(type_, str) else None
    if validator is None:
        raise ProtocolError("unknown_type", f"unknown message type {type_!r}")
    return type_, validator(msg)
