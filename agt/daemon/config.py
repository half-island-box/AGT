"""DaemonConfig — runtime configuration loaded from environment variables."""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class DaemonConfig:
    """All knobs needed to run the daemon. Loaded from env in ``from_env()``."""

    # Server integration (Phase 2).
    server_base_url: str | None = None

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

    @classmethod
    def from_env(cls) -> "DaemonConfig":
        """Build a DaemonConfig from AGT_* environment variables."""
        default = cls()
        return cls(
            server_base_url=os.environ.get("AGT_SERVER_URL"),
            daemon_id=os.environ.get("AGT_DAEMON_ID") or default.daemon_id,
            device_name=os.environ.get("AGT_DEVICE_NAME") or default.device_name,
            runtime_name=os.environ.get("AGT_RUNTIME_NAME", ""),
            max_concurrent_tasks=int(os.environ.get("AGT_MAX_CONCURRENT", str(default.max_concurrent_tasks))),
            agent_timeout=int(os.environ.get("AGT_AGENT_TIMEOUT", str(default.agent_timeout))),
            work_dir_root=Path(
                os.environ.get("AGT_WORK_DIR", str(default.work_dir_root))
            ),
        )


__all__ = ["DaemonConfig"]