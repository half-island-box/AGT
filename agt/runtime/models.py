"""Runtime data models — AgentEntry and Runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class AgentEntry:
    """Information about a single agent CLI executable discovered on this machine."""

    command: str
    path: Path
    version: str
    provider: str
    display_name: str
    default_model: str | None = None
    # How TaskExecutor builds the non-interactive command for this CLI
    # ("claude" → `-p <prompt>`; "codex" → `exec <prompt>`). Mirrors
    # KnownAgent.cli_style; see agt/probe/known_agents.py.
    cli_style: str = "claude"

    def is_available(self) -> bool:
        """Check whether the executable still exists and is runnable."""
        return self.path.exists() and os.access(self.path, os.X_OK)


@dataclass
class Runtime:
    """A runtime declaration: this machine can execute a given provider."""

    name: str
    type: str
    version: str
    status: str  # "online" | "offline" | "unknown"
    agent_entry: AgentEntry
    device_name: str

    def to_dict(self) -> dict:
        """Serialize for server registration payloads (Phase 2)."""
        return {
            "name": self.name,
            "type": self.type,
            "version": self.version,
            "status": self.status,
            "device_name": self.device_name,
        }


__all__ = ["AgentEntry", "Runtime"]