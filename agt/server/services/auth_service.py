"""TaskToken — HMAC-SHA256 signed tokens bound to (agent, task, workspace).

The token format is `<base64url(payload)>.<hex_hmac_sig>` where payload is a
JSON dict containing `sub`, `task_id`, `workspace_id`, `iat`, `exp`.

Used by agent subprocesses to authenticate callbacks to the Server. The
daemon issues a fresh token at spawn time (so each task gets its own short-
lived credential) and injects it into the subprocess via the AGT_TASK_TOKEN
environment variable.

Tokens are NOT JWTs — this is a minimal custom format that avoids pulling
in a JWT library for one signing need. The signature uses hmac.compare_digest
to defend against timing attacks.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime, timedelta, timezone
from typing import Any


def _b64_encode(raw: bytes) -> str:
    """URL-safe base64 without padding."""
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64_decode(s: str) -> bytes:
    """Inverse of _b64_encode; restores padding before decoding."""
    padding = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + padding)


class TaskToken:
    """Static helpers for issuing and verifying task-scoped tokens."""

    @staticmethod
    def create(
        agent_id: str,
        task_id: str,
        workspace_id: str,
        secret: str,
        ttl_hours: int = 24,
    ) -> str:
        """Issue a token. ``secret`` is the shared HMAC key (Server's)."""
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": f"agent:{agent_id}",
            "task_id": task_id,
            "workspace_id": workspace_id,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
        }
        payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{sig}"

    @staticmethod
    def create_server(server_id: str, secret: str, ttl_hours: int = 24 * 365) -> str:
        """Mint a long-lived server credential for the reverse dial (Step 3.5).

        Used by the Server when it dials a remote daemon over WS. The
        daemon verifies this token (via ``TaskToken.verify``) before
        accepting the connection, so any third party cannot impersonate
        the Server. Same HMAC machinery as ``create``; the
        ``sub=server:<id>`` prefix distinguishes this from an agent
        token in audit logs.

        ``ttl_hours`` defaults to a year; rotation happens by Server
        restart (a new token is minted on demand).
        """
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": f"server:{server_id}",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(hours=ttl_hours)).timestamp()),
        }
        payload_b64 = _b64_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        sig = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{payload_b64}.{sig}"

    @staticmethod
    def verify(token: str, secret: str) -> dict[str, Any]:
        """Verify a token and return its payload claims.

        Raises ``PermissionError`` on:
          - malformed input (no "." separator)
          - bad signature
          - expired (``exp`` in the past)
        """
        if "." not in token:
            raise PermissionError("malformed token")
        payload_b64, sig = token.rsplit(".", 1)
        expected = hmac.new(secret.encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise PermissionError("invalid signature")

        try:
            payload = json.loads(_b64_decode(payload_b64))
        except (ValueError, UnicodeDecodeError) as exc:
            raise PermissionError(f"invalid payload: {exc}") from exc

        exp = payload.get("exp")
        if not isinstance(exp, int):
            raise PermissionError("missing exp claim")
        if exp < int(datetime.now(timezone.utc).timestamp()):
            raise PermissionError("token expired")

        return payload


__all__ = ["TaskToken"]