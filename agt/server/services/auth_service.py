"""TaskToken — JWT-based tokens bound to (agent, task, workspace) or server/daemon identity.

The token format is a standard JWT signed with HS256. We use a minimal custom
wrapper so callers don't need to import PyJWT directly, while still benefiting
from standard ``exp``, ``iat``, ``aud``, ``iss`` claims.

Used by:
- Agent subprocesses to authenticate callbacks to the Server (``X-Task-Token``).
- The Server to authenticate reverse-dial WebSocket connections to daemons
  (``X-Server-Token``).
- Daemons to authenticate themselves back to the Server over the same WS link
  (``daemon_token`` embedded in the ``heartbeat_ack`` frame).

Tokens are issued on demand and short-lived by convention; rotation happens by
setting a fresh ``exp`` or rolling ``AGT_SERVER_SECRET``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import jwt


# Algorithm whitelist. We deliberately do NOT support 'none' or any other
# algorithm to keep the trust boundary small.
_ALGORITHM = "HS256"


class TaskToken:
    """Static helpers for issuing and verifying task/server/daemon tokens."""

    @staticmethod
    def create(
        agent_id: str,
        task_id: str,
        workspace_id: str,
        secret: str,
        ttl_hours: int = 24,
    ) -> str:
        """Issue a task-scoped token for agent subprocess callbacks."""
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": f"agent:{agent_id}",
            "iss": "agt-server",
            "task_id": task_id,
            "workspace_id": workspace_id,
            "iat": now,
            "exp": now + timedelta(hours=ttl_hours),
        }
        return jwt.encode(payload, secret, algorithm=_ALGORITHM)

    @staticmethod
    def create_server(
        server_id: str,
        secret: str,
        daemon_id: str | None = None,
        ttl_hours: int = 24 * 365,
    ) -> str:
        """Mint a server credential for the reverse dial.

        When ``daemon_id`` is provided, the token carries an ``aud`` claim
        binding it to that specific daemon. The daemon MUST reject tokens
        whose ``aud`` does not match its own identity.
        """
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": f"server:{server_id}",
            "iss": "agt-server",
            "iat": now,
            "exp": now + timedelta(hours=ttl_hours),
        }
        if daemon_id is not None:
            payload["aud"] = daemon_id
        return jwt.encode(payload, secret, algorithm=_ALGORITHM)

    @staticmethod
    def create_daemon(
        daemon_id: str,
        url: str,
        secret: str,
        ttl_hours: int = 24 * 365,
    ) -> str:
        """Mint a daemon credential used to authenticate back to the Server.

        The ``sub`` claim encodes the daemon identity so the Server can verify
        it connected to the daemon it expected.
        """
        now = datetime.now(timezone.utc)
        payload: dict[str, Any] = {
            "sub": f"daemon:{daemon_id}",
            "iss": "agt-daemon",
            "url": url,
            "iat": now,
            "exp": now + timedelta(hours=ttl_hours),
        }
        return jwt.encode(payload, secret, algorithm=_ALGORITHM)

    @staticmethod
    def verify(
        token: str,
        secret: str,
        *,
        audience: str | None = None,
        issuer: str | None = None,
    ) -> dict[str, Any]:
        """Verify a token and return its payload claims.

        Raises ``PermissionError`` on any validation failure so callers can
        continue treating token problems uniformly.
        """
        try:
            return jwt.decode(
                token,
                secret,
                algorithms=[_ALGORITHM],
                audience=audience,
                issuer=issuer,
            )
        except jwt.ExpiredSignatureError as exc:
            raise PermissionError("token expired") from exc
        except jwt.InvalidAudienceError as exc:
            raise PermissionError("invalid audience") from exc
        except jwt.InvalidIssuerError as exc:
            raise PermissionError("invalid issuer") from exc
        except jwt.InvalidTokenError as exc:
            raise PermissionError(f"invalid token: {exc}") from exc


def verify_task_token_claims(token: str, secret: str) -> dict[str, Any]:
    """Verify an agent callback token and require its task binding."""
    claims = TaskToken.verify(token, secret, issuer="agt-server")
    task_id = claims.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise PermissionError("token is not task scoped")
    return claims


def bearer_token_from_header(value: str | None) -> str | None:
    """Extract a non-empty bearer token from an HTTP Authorization value."""
    if not isinstance(value, str):
        return None
    scheme, separator, token = value.strip().partition(" ")
    if not separator or scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


__all__ = [
    "TaskToken",
    "verify_task_token_claims",
    "bearer_token_from_header",
]
