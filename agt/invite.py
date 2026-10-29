"""Invite codes — the one-paste payload for ``agt daemon init``.

A reverse-dial daemon needs four things before it can accept a Server's
dial: the shared secret, its own public URL, a stable ``daemon_id`` and
listen-mode settings. Making the user copy those by hand is where setup
usually goes wrong (a single character of URL drift silently changes the
uuid5 identity). An invite code carries all four in one string:

    agt1.<base64url(utf8(json))>

The prefix is versioned so a future format can be introduced without
ambiguity — ``decode_invite`` rejects anything it does not understand
instead of guessing.

The payload embeds the Server's HMAC secret. Treat an invite code with
the same care as an SSH private key; see
``docs/impl/daemon-pairing-and-secret-design.md`` §10.
"""

from __future__ import annotations

import base64
import binascii
import json
from typing import Any

INVITE_PREFIX = "agt1"
INVITE_VERSION = 1

_REQUIRED_FIELDS = ("secret", "url", "id")


class InviteError(ValueError):
    """Raised for malformed, unsupported or incomplete invite codes."""


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    # Restore the padding stripped at encode time.
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def encode_invite(
    *,
    secret: str,
    url: str,
    daemon_id: str,
    name: str = "",
    ws_port: int | None = None,
    server_url: str | None = None,
) -> str:
    """Build an invite code from the pieces a daemon needs."""
    if not secret:
        raise InviteError("secret is required")
    if not url:
        raise InviteError("url is required")
    if not daemon_id:
        raise InviteError("daemon_id is required")

    payload: dict[str, Any] = {
        "v": INVITE_VERSION,
        "secret": secret,
        "url": url,
        "id": daemon_id,
    }
    if name:
        payload["name"] = name
    if ws_port is not None:
        payload["ws_port"] = int(ws_port)
    if server_url:
        payload["server_url"] = server_url

    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return f"{INVITE_PREFIX}.{_b64encode(raw.encode('utf-8'))}"


def decode_invite(code: str) -> dict[str, Any]:
    """Parse an invite code back into its payload.

    Raises :class:`InviteError` for a wrong prefix, an unknown version or
    missing/ill-typed fields. Never returns a partially trusted result.
    """
    text = (code or "").strip()
    if not text.startswith(f"{INVITE_PREFIX}."):
        raise InviteError(
            "unrecognised invite code (expected it to start with "
            f"'{INVITE_PREFIX}.')"
        )
    encoded = text[len(INVITE_PREFIX) + 1 :]
    if not encoded:
        raise InviteError("invite code is empty")
    try:
        raw = _b64decode(encoded)
    except (binascii.Error, ValueError) as exc:
        raise InviteError(f"invite code is not valid base64url: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InviteError(f"invite code payload is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise InviteError("invite code payload must be an object")

    version = payload.get("v")
    if version != INVITE_VERSION:
        raise InviteError(
            f"unsupported invite version {version!r}; upgrade agt "
            f"(this build understands v{INVITE_VERSION})"
        )

    for field in _REQUIRED_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise InviteError(f"invite code is missing '{field}'")

    url = payload["url"]
    if not url.startswith(("ws://", "wss://")):
        raise InviteError("invite url must start with ws:// or wss://")

    ws_port = payload.get("ws_port")
    if ws_port is not None and (
        not isinstance(ws_port, int) or isinstance(ws_port, bool)
    ):
        raise InviteError("invite ws_port must be an integer")

    return payload


__all__ = [
    "INVITE_PREFIX",
    "INVITE_VERSION",
    "InviteError",
    "decode_invite",
    "encode_invite",
]
