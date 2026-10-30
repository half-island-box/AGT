"""Shared loading for the Server/Daemon HMAC signing secret.

Environment configuration always wins. When no environment value is set,
both processes read the same canonical file so task tokens issued by a
daemon can be verified by the server without duplicating shell exports.
"""

from __future__ import annotations

import os
import secrets
from collections.abc import Mapping
from pathlib import Path


def default_server_secret_path() -> Path:
    """Return the canonical server secret file path."""
    return Path.home() / ".agt" / "server.secret"


def load_server_secret(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Load the secret from ``AGT_SERVER_SECRET`` or the canonical file."""
    env = os.environ if environ is None else environ
    env_secret = env.get("AGT_SERVER_SECRET", "").strip()
    if env_secret:
        return env_secret

    path = default_server_secret_path()
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


__all__ = [
    "default_server_secret_path",
    "ensure_server_secret",
    "load_server_secret",
]


def ensure_server_secret(
    environ: Mapping[str, str] | None = None,
) -> str:
    """Return a usable secret, creating ``~/.agt/server.secret`` if needed.

    Resolution order matches :func:`load_server_secret`, with one addition:
    when neither the environment variable nor the file provides a value, a
    fresh random secret is generated **and persisted** with ``0600``.

    An explicit ``AGT_SERVER_SECRET`` is never written to disk — the
    operator asked for that value to be supplied at runtime, so mirroring
    it into the file would be a surprise (and a second copy to rotate).

    Raises ``OSError`` only when the file must be created and cannot be.
    """
    existing = load_server_secret(environ)
    if existing:
        return existing

    path = default_server_secret_path()
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        # Filesystems without POSIX modes (e.g. some mounts) still work;
        # the value is valid, only the permission tightening failed.
        pass
    return secret
