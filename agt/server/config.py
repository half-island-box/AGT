"""ServerConfig — runtime configuration for the agt-server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..ids import daemon_id_from_url


class ServerConfigError(RuntimeError):
    """Raised when the server configuration is incomplete or invalid."""


def _default_secret_path() -> Path:
    """Return the canonical server secret file path."""
    return Path.home() / ".agt" / "server.secret"


def _load_server_secret() -> str:
    """Load the server secret: env var first, then ~/.agt/server.secret.

    Returns the secret string if found, otherwise an empty string.
    """
    env_secret = os.environ.get("AGT_SERVER_SECRET", "").strip()
    if env_secret:
        return env_secret
    path = _default_secret_path()
    if path.exists():
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


@dataclass
class DaemonTarget:
    """One configured remote daemon (reverse-dial target).

    ``daemon_id`` is derived from ``url`` (uuid5 in the shared
    namespace — same derivation the daemon itself uses), so the file
    only needs ``name url``; the id is never stored in it.

    ``capabilities`` are optional static labels declared in a third
    ``,``-separated field (``name url cap1,cap2``). They are descriptive
    hints for routing/display (phase A) — the live runtime catalog that
    agents introspect is deferred to a later phase.
    """

    daemon_id: str
    name: str
    url: str  # ws://host:port (or wss://)
    capabilities: tuple[str, ...] = ()  # optional static labels


def _parse_daemons_file(path: str | None) -> list[DaemonTarget]:
    """Parse ``~/.agt/daemons`` — one ``name url [cap1,cap2,...]`` per line.

    Format (intentionally minimal — no extra dependency):

        # Comments start with hash
        daemon-cloud-01 ws://1.2.3.4:8422 containers,logs
        daemon-cloud-02 ws://5.6.7.8:8422

    The third field (optional) declares a daemon's static capability
    labels, ``,``-separated. The id is never read from the file — it is
    derived from the URL via :func:`daemon_id_from_url` (uuid5, shared
    namespace), matching what the daemon itself computes for its own URL.

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
        name = parts[0].strip()
        rest = parts[1].strip()
        tokens = rest.split(None, 1)
        url = tokens[0].strip()
        caps_raw = tokens[1].strip() if len(tokens) == 2 else ""
        if not name or not url:
            log.warning("daemons: ignoring empty field: %r", raw)
            continue
        if not (url.startswith(("ws://", "wss://"))):
            log.warning(
                "daemons: ignoring %s — URL must start with ws:// or wss://: %r",
                name, url,
            )
            continue
        capabilities = tuple(c.strip() for c in caps_raw.split(",") if c.strip())
        out.append(
            DaemonTarget(
                daemon_id=daemon_id_from_url(url),
                name=name,
                url=url,
                capabilities=capabilities,
            )
        )
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

    # PID file written by the lifespan when set; ``agt server stop`` reads it
    # back to send SIGTERM. ``None`` (the default) means no pid file is
    # written — the foreground CLI path leaves no trace, while the detached
    # CLI path sets this to ~/.agt/server.pid.
    pid_file: str | None = None

    # Reverse-dial daemon list (Step 3.5). ``daemons_path`` is read once
    # at construction; ``daemons`` carries the parsed targets.
    daemons_path: str = field(
        default_factory=lambda: str(Path.home() / ".agt" / "daemons")
    )
    daemons: list[DaemonTarget] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "ServerConfig":
        secret = _load_server_secret()
        if not secret:
            raise ServerConfigError(
                "AGT_SERVER_SECRET is not configured. "
                "Run `agt server init` to generate one, or set the "
                "AGT_SERVER_SECRET environment variable."
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


__all__ = ["ServerConfig", "DaemonTarget", "ServerConfigError"]
