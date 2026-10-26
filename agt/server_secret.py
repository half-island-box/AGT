"""Shared loading for the Server/Daemon HMAC signing secret.

Environment configuration always wins. When no environment value is set,
both processes read the same canonical file so task tokens issued by a
daemon can be verified by the server without duplicating shell exports.
"""

from __future__ import annotations

import os
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


__all__ = ["default_server_secret_path", "load_server_secret"]
