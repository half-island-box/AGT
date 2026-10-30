"""ServerConfig — runtime configuration for the agt-server."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..daemons_file import parse_daemons_line
from ..ids import daemon_id_from_url
from ..server_secret import (
    default_server_secret_path as _default_secret_path,
    load_server_secret as _load_server_secret,
)


class ServerConfigError(RuntimeError):
    """Raised when the server configuration is incomplete or invalid."""


@dataclass
class DaemonTarget:
    """One configured remote daemon (reverse-dial target).

    ``daemon_id`` is explicit when the line carries ``id=<uuid>``;
    otherwise it is derived from ``url`` (uuid5 in the shared namespace —
    same derivation the daemon itself uses), so a legacy file only needs
    ``name url``.

    ``capabilities`` are optional static labels declared in the trailing
    ``,``-separated token(s) (``name url [id=...] cap1,cap2``). They are
    descriptive hints for routing/display (phase A) — the live runtime
    catalog that agents introspect is deferred to a later phase.
    """

    daemon_id: str
    name: str
    url: str  # ws://host:port (or wss://)
    capabilities: tuple[str, ...] = ()  # optional static labels


def _parse_daemons_file(path: str | None) -> list[DaemonTarget]:
    """Parse ``~/.agt/daemons`` — one ``name url [id=<uuid>] [caps]`` per line.

    Format (intentionally minimal — no extra dependency):

        # Comments start with hash
        daemon-cloud-01 ws://1.2.3.4:8422 id=<uuid> containers,logs
        daemon-cloud-02 ws://5.6.7.8:8422

    The ``id=`` field is optional. When present it wins; otherwise the id
    is derived from the URL via :func:`daemon_id_from_url` (uuid5, shared
    namespace), matching what the daemon itself computes for its own URL.
    An explicit id decouples a daemon's identity from the exact spelling
    of its URL — see ``docs/impl/daemon-pairing-and-secret-design.md`` §5.2.

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
        entry = parse_daemons_line(line)
        if entry is None:
            log.warning("daemons: ignoring malformed line: %r", raw)
            continue
        name, url = entry.name, entry.url
        if not name or not url:
            log.warning("daemons: ignoring empty field: %r", raw)
            continue
        if not (url.startswith(("ws://", "wss://"))):
            log.warning(
                "daemons: ignoring %s — URL must start with ws:// or wss://: %r",
                name, url,
            )
            continue
        out.append(
            DaemonTarget(
                daemon_id=entry.daemon_id or daemon_id_from_url(url),
                name=name,
                url=url,
                capabilities=entry.capabilities,
            )
        )
    return out


@dataclass
class ServerConfig:
    """Configuration for the FastAPI server process."""

    host: str = "0.0.0.0"
    port: int = 8420
    db_path: str = field(default_factory=lambda: str(Path.home() / ".agt" / "agt.db"))
    # Legacy Phase 1 migration input. Retained for CLI/config compatibility,
    # but the Server no longer reads or writes this file automatically.
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
    # Browser console hosting. Loopback-only by default; non-loopback
    # binds must opt in explicitly until the remote-console auth layer is
    # implemented.
    console_enabled: bool = True
    console_allow_remote: bool = False
    console_assets_dir: str = field(
        default_factory=lambda: str(
            Path(__file__).resolve().parent.parent / "web_console" / "assets"
        )
    )
    console_workspace_roots: list[str] = field(
        default_factory=lambda: [str(Path.home())]
    )
    # Chat attachment limits (user → agent input uploads). The Server owns
    # the bytes; these caps are enforced before anything is written.
    max_upload_bytes: int = 32 * 1024 * 1024
    upload_ttl_days: int = 7

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
        workspace_roots = [
            str(Path(item).expanduser().resolve())
            for item in os.environ.get(
                "AGT_CONSOLE_WORKSPACE_ROOTS",
                str(Path.home()),
            ).split(os.pathsep)
            if item.strip()
        ]
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
            console_enabled=os.environ.get(
                "AGT_CONSOLE_ENABLED", "1"
            ).strip().lower() not in {"0", "false", "no", "off"},
            console_allow_remote=os.environ.get(
                "AGT_CONSOLE_ALLOW_REMOTE", "0"
            ).strip().lower() in {"1", "true", "yes", "on"},
            console_assets_dir=os.environ.get(
                "AGT_CONSOLE_ASSETS",
                str(Path(__file__).resolve().parent.parent / "web_console" / "assets"),
            ),
            console_workspace_roots=workspace_roots,
            max_upload_bytes=int(
                os.environ.get("AGT_MAX_UPLOAD_BYTES", str(32 * 1024 * 1024))
            ),
            upload_ttl_days=int(os.environ.get("AGT_UPLOAD_TTL_DAYS", "7")),
        )


__all__ = ["ServerConfig", "DaemonTarget", "ServerConfigError"]
