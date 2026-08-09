"""Default known-agent CLI list, plus user extension via AGT_EXTRA_AGENTS env var."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownAgent:
    """A known agent CLI command registered in the default list."""

    command: str
    provider: str
    display_name: str


# Built-in agents maintained by the project.
KNOWN_AGENTS: list[KnownAgent] = [
    KnownAgent("claude", "claude", "Claude Code"),
    KnownAgent("codex", "codex", "Codex CLI"),
    KnownAgent("cursor-agent", "cursor", "Cursor Agent"),
    KnownAgent("copilot", "copilot", "GitHub Copilot"),
    KnownAgent("qwen", "qwen", "Qwen CLI"),
    KnownAgent("kimi", "kimi", "Kimi CLI"),
    KnownAgent("opencode", "opencode", "OpenCode CLI"),
]


_EXTRA_ENV_VAR = "AGT_EXTRA_AGENTS"


def _parse_extra(value: str) -> list[KnownAgent]:
    """Parse a comma-separated `name:provider:display_name` list."""
    out: list[KnownAgent] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) != 3:
            # Skip malformed entries silently — the env var is best-effort.
            continue
        command, provider, display_name = (p.strip() for p in parts)
        if command and provider and display_name:
            out.append(KnownAgent(command, provider, display_name))
    return out


def get_all_known_agents() -> list[KnownAgent]:
    """Return built-in agents + any user-defined extras."""
    extra_value = os.environ.get(_EXTRA_ENV_VAR, "")
    return list(KNOWN_AGENTS) + _parse_extra(extra_value)


__all__ = ["KnownAgent", "KNOWN_AGENTS", "get_all_known_agents"]