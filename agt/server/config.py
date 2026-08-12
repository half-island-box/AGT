"""ServerConfig — runtime configuration for the agt-server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ServerConfig:
    """Configuration for the FastAPI server process."""

    host: str = "127.0.0.1"
    port: int = 8420
    db_path: str = field(default_factory=lambda: str(Path.home() / ".agt" / "agt.db"))
    sessions_json_path: str = field(
        default_factory=lambda: str(Path.home() / ".agt" / "sessions.json")
    )
    secret: str = ""  # HMAC signing secret for task-scoped tokens.

    @classmethod
    def from_env(cls) -> "ServerConfig":
        secret = os.environ.get("AGT_SERVER_SECRET", "")
        if not secret:
            raise RuntimeError(
                "AGT_SERVER_SECRET environment variable is required "
                "(generate with: openssl rand -hex 32)"
            )
        return cls(
            host=os.environ.get("AGT_SERVER_HOST", "127.0.0.1"),
            port=int(os.environ.get("AGT_SERVER_PORT", "8420")),
            db_path=os.environ.get(
                "AGT_DB_PATH", str(Path.home() / ".agt" / "agt.db")
            ),
            sessions_json_path=os.environ.get(
                "AGT_SESSIONS_JSON",
                str(Path.home() / ".agt" / "sessions.json"),
            ),
            secret=secret,
        )


__all__ = ["ServerConfig"]