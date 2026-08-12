"""DaemonConfig — runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
import secrets
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonConfig:
    """All knobs needed to run the daemon. Loaded from env in ``from_env()``."""

    # Server integration (Phase 2).
    server_base_url: str | None = None
    # Shared HMAC secret with the Server; required to sign task tokens injected
    # into spawned agent subprocesses (Phase 3). Empty disables token injection.
    server_secret: str = ""

    # Local identity.
    daemon_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    device_name: str = field(default_factory=socket.gethostname)
    runtime_name: str = ""

    # Execution.
    max_concurrent_tasks: int = 1
    agent_timeout: int = 600  # seconds
    work_dir_root: Path = field(
        default_factory=lambda: Path.home() / ".agt" / "workspaces"
    )

    # Local resource server (Phase 2). Port 0 = disabled.
    daemon_port: int = 8421
    # Shared secret with spawned agent subprocesses for X-Daemon-Local-Token.
    # Random per-launch by default; can be pinned via AGT_DAEMON_LOCAL_TOKEN.
    local_token: str = ""

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
        return cls(
            server_base_url=os.environ.get("AGT_SERVER_URL"),
            server_secret=os.environ.get("AGT_SERVER_SECRET", ""),
            daemon_id=os.environ.get("AGT_DAEMON_ID") or default.daemon_id,
            device_name=os.environ.get("AGT_DEVICE_NAME") or default.device_name,
            runtime_name=os.environ.get("AGT_RUNTIME_NAME", ""),
            max_concurrent_tasks=int(os.environ.get("AGT_MAX_CONCURRENT", str(default.max_concurrent_tasks))),
            agent_timeout=int(os.environ.get("AGT_AGENT_TIMEOUT", str(default.agent_timeout))),
            work_dir_root=Path(
                os.environ.get("AGT_WORK_DIR", str(default.work_dir_root))
            ),
            daemon_port=int(os.environ.get("AGT_DAEMON_PORT", str(default.daemon_port))),
            local_token=os.environ.get("AGT_DAEMON_LOCAL_TOKEN") or secrets.token_urlsafe(32),
        )


__all__ = ["DaemonConfig"]