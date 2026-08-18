"""ServerConfig — runtime configuration for the agt-server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonTarget:
    """One configured remote daemon (reverse-dial target)."""

    daemon_id: str
    url: str  # ws://host:port (or wss://)


def _parse_daemons_file(path: str | None) -> list[DaemonTarget]:
    """Parse ``~/.agt/daemons`` — one ``daemon_id URL`` per line.

    Format (intentionally minimal — no extra dependency):

        # Comments start with hash
        daemon-cloud-01 ws://1.2.3.4:8422
        daemon-cloud-02 ws://5.6.7.8:8422

    Empty lines are skipped. Missing file → empty list. Malformed lines
    are logged and skipped (we don't want a typo to take the server
    down).
    """
    if not path:
        return []
    p = Path(path).expanduser()
    if not p.exists():
        return []
    out: list[DaemonTarget] = []
    import logging

    log = logging.getLogger("agt.server.config")
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            log.warning("daemons: ignoring malformed line: %r", raw)
            continue
        daemon_id, url = parts[0].strip(), parts[1].strip()
        if not daemon_id or not url:
            log.warning("daemons: ignoring empty field: %r", raw)
            continue
        if not (url.startswith(("ws://", "wss://"))):
            log.warning(
                "daemons: ignoring %s — URL must start with ws:// or wss://: %r",
                daemon_id, url,
            )
            continue
        out.append(DaemonTarget(daemon_id=daemon_id, url=url))
    return out


@dataclass
class ServerConfig:
    """Configuration for the FastAPI server process."""

    host: str = "0.0.0.0"
    port: int = 8420
    db_path: str = field(default_factory=lambda: str(Path.home() / ".agt" / "agt.db"))
    sessions_json_path: str = field(
        default_factory=lambda: str(Path.home() / ".agt" / "sessions.json")
    )
    secret: str = ""  # HMAC signing secret for task-scoped tokens.

    # Reverse-dial daemon list (Step 3.5). ``daemons_path`` is read once
    # at construction; ``daemons`` carries the parsed targets.
    daemons_path: str = field(
        default_factory=lambda: str(Path.home() / ".agt" / "daemons")
    )
    daemons: list[DaemonTarget] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "ServerConfig":
        secret = os.environ.get("AGT_SERVER_SECRET", "")
        if not secret:
            raise RuntimeError(
                "AGT_SERVER_SECRET environment variable is required "
                "(generate with: openssl rand -hex 32)"
            )
        daemons_path = os.environ.get(
            "AGT_DAEMONS_FILE", str(Path.home() / ".agt" / "daemons")
        )
        return cls(
            host=os.environ.get("AGT_SERVER_HOST", "0.0.0.0"),
            port=int(os.environ.get("AGT_SERVER_PORT", "8420")),
            db_path=os.environ.get(
                "AGT_DB_PATH", str(Path.home() / ".agt" / "agt.db")
            ),
            sessions_json_path=os.environ.get(
                "AGT_SESSIONS_JSON",
                str(Path.home() / ".agt" / "sessions.json"),
            ),
            secret=secret,
            daemons_path=daemons_path,
            daemons=_parse_daemons_file(daemons_path),
        )


__all__ = ["ServerConfig", "DaemonTarget"]