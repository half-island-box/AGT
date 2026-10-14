"""DaemonConfig — runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..ids import AGT_DAEMON_NAMESPACE, daemon_id_from_url
from ..net import host_of_url as _host_of_url, is_local_host as _is_local_host

# Re-exported for backward compatibility (CLI and tests import it from
# here); the canonical definition lives in ``agt.ids``.
_ = (AGT_DAEMON_NAMESPACE, daemon_id_from_url)


def _read_daemons_file() -> list[tuple[str, str]]:
    """Parse ``~/.agt/daemons`` — one ``name url`` per line.

    Mirrors the Server's ``_parse_daemons_file`` shape; here we only need
    (name, url) pairs. Missing file / malformed lines are skipped.
    """
    path = Path.home() / ".agt" / "daemons"
    if not path.exists():
        return []
    out: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        name, url = parts[0].strip(), parts[1].strip()
        if name and url:
            out.append((name, url))
    return out


def _port_of_url(url: str) -> str | None:
    """Extract the port from a ``ws://host:port`` / ``http://host:port`` URL."""
    try:
        after_scheme = url.split("://", 1)[1]
        host_part = after_scheme.split("/", 1)[0]
        if ":" in host_part:
            return host_part.rsplit(":", 1)[1]
    except (IndexError, ValueError):
        pass
    return None


def _find_self_daemon(ws_listen_port: int) -> tuple[str, str] | None:
    """Return ``(name, url)`` for this daemon from ``~/.agt/daemons``.

    Rules (always require BOTH a port match and a local host):
    - any entry whose URL port equals ``ws_listen_port`` AND whose host
      is this machine → that entry
    - otherwise → None

    A single-entry file is NOT assumed to be this machine — that's the
    foot-gun that made a host with only a cloud registration (in
    ``~/.agt/daemons``) appear to be the cloud machine itself, with
    uuid5(NS, cloud_url) as its own daemon_id. The dual check also
    disambiguates a cloud entry and the auto-registered local entry
    when both use port 8422: ``agt daemon start`` appends
    ``local ws://0.0.0.0:<port>`` at startup, and only that entry has
    a local host.
    """
    for name, url in _read_daemons_file():
        if _port_of_url(url) == str(ws_listen_port) and _is_local_host(
            _host_of_url(url)
        ):
            return (name, url)
    return None


def _parse_allowlist(raw: str | None) -> list[Path]:
    """Parse ``AGT_AGENT_CWD_ALLOWLIST`` into a list of absolute Path roots.

    Format: colon-separated absolute paths. Empty / unset → ``[Path.home()]``
    (the safe default). A single ``/`` is allowed and means "anywhere" —
    same semantics as giving the daemon full filesystem access; only set
    this when you trust every submitter.
    """
    if not raw or not raw.strip():
        return [Path.home()]
    out: list[Path] = []
    for part in raw.split(":"):
        part = part.strip()
        if not part:
            continue
        out.append(Path(part))
    return out or [Path.home()]


def _is_path_under(child: Path, parent: Path) -> bool:
    """Return True if ``child`` is the same as or inside ``parent``.

    Both are resolved (``Path.resolve``) first to defeat symlink tricks
    and relative paths. Used by the agent_cwd allowlist check.
    """
    try:
        child_r = child.resolve()
        parent_r = parent.resolve()
    except (OSError, RuntimeError):
        return False
    if child_r == parent_r:
        return True
    try:
        child_r.relative_to(parent_r)
        return True
    except ValueError:
        return False


def _load_daemon_id(url: str | None) -> str:
    """Stable daemon identity.

    Priority:
    1. ``AGT_DAEMON_ID`` env (explicit override, highest).
    2. ``uuid5(AGT_DAEMON_NAMESPACE, url)`` — deterministic, stable across
       restarts; a changed URL (address moved) legitimately changes the id.
    3. Fresh uuid4 — transient, NOT persisted. Only hit when the daemon
       starts with no env override and no self entry in ``~/.agt/daemons``
       (i.e. started outside ``agt daemon start`` on a bare machine);
       the id will not survive a restart in that case.

    The old ``~/.agt/daemon_id`` persistent-file fallback was removed:
    it was almost never written (the env and url branches return before
    reaching it) while the CLI's local-daemon detection depended on it —
    which therefore never worked. Local detection now goes through
    ``~/.agt/daemons`` (see ``_ensure_local_daemons_entry``).
    """
    env = os.environ.get("AGT_DAEMON_ID")
    if env:
        return env
    if url:
        return daemon_id_from_url(url)
    return str(uuid.uuid4())[:8]


@dataclass
class DaemonConfig:
    """All knobs needed to run the daemon. Loaded from env in ``from_env()``."""

    # Server integration (Phase 2).
    server_base_url: str | None = None
    # Shared HMAC secret with the Server; required to sign task tokens injected
    # into spawned agent subprocesses (Phase 3). Empty disables token injection.
    server_secret: str = ""

    # Local identity.
    # ``daemon_id`` is the stable machine/identity key (uuid5 of the daemon's
    # URL when available); ``daemon_name`` is the human-readable label (from
    # ``~/.agt/daemons`` name, or the device name). name may change without
    # affecting id; changing the URL changes the id (address moved).
    # ``daemon_url`` is the URL this daemon advertises for itself, taken from
    # the matching entry in ``~/.agt/daemons`` (used in the daemon_token's
    # ``url`` claim for auditability).
    daemon_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    daemon_name: str = field(default_factory=socket.gethostname)
    daemon_url: str = ""
    device_name: str = field(default_factory=socket.gethostname)
    runtime_name: str = ""

    # Execution.
    max_concurrent_tasks: int = 1
    agent_timeout: int = 600  # seconds
    work_dir_root: Path = field(
        default_factory=lambda: Path.home() / ".agt" / "workspaces"
    )
    # cwd injection (Phase 4 workdir supplement): list of root directories
    # that ``agent_cwd`` from a claim payload is allowed to land under.
    # Default: ``[Path.home()]``. Override via ``AGT_AGENT_CWD_ALLOWLIST``
    # (colon-separated absolute paths; ``/`` = allow anywhere). May be
    # ``None`` for older ``DaemonConfig(...)`` callers — the daemon falls
    # back to ``[Path.home()]`` at use time (see ``Daemon.run``).
    agent_cwd_allowlist: list[Path] | None = None

    # Local resource server (Phase 2). Port 0 = disabled.
    daemon_port: int = 8421
    # Shared secret with spawned agent subprocesses for X-Daemon-Local-Token.
    # Random per-launch by default; can be pinned via AGT_DAEMON_LOCAL_TOKEN.
    local_token: str = ""

    # Reverse-dial mode (Step 3.5). When True, the daemon listens on
    # ``ws_listen_port`` for the Server to dial in, instead of dialing
    # the Server itself. When False (default), the daemon claims via
    # plain HTTP — the local-daemon path.
    listen_mode: bool = False
    ws_listen_port: int = 8422  # AGT_DAEMON_WS_PORT

    # Phase B: reverse-dial proxy MCP server port (127.0.0.1 only). The
    # remote daemon serves the catalog proxy here; local agents instead
    # reach the Server's ``/mcp`` directly, so this is only bound in
    # ``listen_mode``.
    mcp_port: int = 8428  # AGT_DAEMON_MCP_PORT

    @property
    def server_url(self) -> str:
        """Convenience accessor for the Server base URL.

        Returns ``AGT_SERVER_URL`` if set, otherwise a localhost default
        pointing at the well-known Server port.
        """
        if self.server_base_url:
            return self.server_base_url
        return os.environ.get("AGT_SERVER_URL", "http://127.0.0.1:8420")

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        """Build a DaemonConfig from AGT_* environment variables."""
        default = cls()
        listen_mode = os.environ.get("AGT_LISTEN_MODE", "0").strip() in ("1", "true", "yes")
        ws_listen_port = int(os.environ.get("AGT_DAEMON_WS_PORT", str(default.ws_listen_port)))
        self_entry = _find_self_daemon(ws_listen_port)
        daemon_url = self_entry[1] if self_entry else None
        # Human-readable label: explicit env, daemons-file name, else "local"
        # (local daemons not registered in ~/.agt/daemons default to "local").
        daemon_name = (
            os.environ.get("AGT_DAEMON_NAME")
            or (self_entry[0] if self_entry else None)
            or "local"
        )
        daemon_id = _load_daemon_id(daemon_url)
        return cls(
            server_base_url=os.environ.get("AGT_SERVER_URL"),
            server_secret=os.environ.get("AGT_SERVER_SECRET", ""),
            daemon_id=daemon_id,
            daemon_name=daemon_name,
            daemon_url=daemon_url or "",
            device_name=os.environ.get("AGT_DEVICE_NAME") or default.device_name,
            runtime_name=os.environ.get("AGT_RUNTIME_NAME", ""),
            max_concurrent_tasks=int(os.environ.get("AGT_MAX_CONCURRENT", str(default.max_concurrent_tasks))),
            agent_timeout=int(os.environ.get("AGT_AGENT_TIMEOUT", str(default.agent_timeout))),
            work_dir_root=Path(
                os.environ.get("AGT_WORK_DIR", str(default.work_dir_root))
            ),
            agent_cwd_allowlist=_parse_allowlist(
                os.environ.get("AGT_AGENT_CWD_ALLOWLIST"),
            ),
            daemon_port=int(os.environ.get("AGT_DAEMON_PORT", str(default.daemon_port))),
            local_token=os.environ.get("AGT_DAEMON_LOCAL_TOKEN") or secrets.token_urlsafe(32),
            listen_mode=listen_mode,
            ws_listen_port=ws_listen_port,
            mcp_port=int(os.environ.get("AGT_DAEMON_MCP_PORT", str(default.mcp_port))),
        )


__all__ = [
    "DaemonConfig",
    "AGT_DAEMON_NAMESPACE",
    "_load_daemon_id",
    "_find_self_daemon",
    "_host_of_url",
    "_is_local_host",
    "_parse_allowlist",
    "_is_path_under",
]