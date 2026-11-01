"""Default known-agent CLI list, plus user extension via AGT_EXTRA_AGENTS env var."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class KnownAgent:
    """A known agent CLI command registered in the default list.

    ``cli_style`` selects how ``TaskExecutor`` builds the non-interactive
    command line for this agent:

    - ``"claude"`` (default): ``<bin> [--resume <id>] -p <prompt>``
      — Claude Code and most ``-p``-style CLIs.
    - ``"codex"``: ``<bin> exec [resume <id>] <prompt>``
      — Codex CLI, whose non-interactive mode is the ``exec``
      subcommand with a positional prompt.

    ``stream_format`` (Phase 5) declares the streaming event format the
    provider's CLI emits when ``agt`` asks for it. The executor reads this
    only for documentation / introspection; the actual decision of
    whether to enable streaming is made by
    :func:`agt.runtime.registry.get_adapter` so providers that have not
    been wired in keep working unchanged. Recognised values:

    - ``"none"`` — provider does not stream (default)
    - ``"claude_stream_json"`` — Claude Code ``--output-format stream-json``
    """

    command: str
    provider: str
    display_name: str
    cli_style: str = "claude"
    stream_format: str = "none"


# Built-in agents maintained by the project.
KNOWN_AGENTS: list[KnownAgent] = [
    KnownAgent("claude", "claude", "Claude Code", stream_format="claude_stream_json"),
    KnownAgent("codex", "codex", "Codex CLI", cli_style="codex"),
]


_EXTRA_ENV_VAR = "AGT_EXTRA_AGENTS"


def _parse_extra(value: str) -> list[KnownAgent]:
    """Parse a comma-separated list.

    Phase 5 accepts an optional fourth ``stream_format`` field:

        ``cmd:provider:Display[:stream_format]``

    Entries with 3 parts stay on the legacy default (``stream_format="none"``);
    entries with 4 parts opt in to a streaming format. Anything else is
    skipped silently — the env var is best-effort.
    """
    out: list[KnownAgent] = []
    for chunk in value.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) not in (3, 4):
            continue
        command = parts[0].strip()
        provider = parts[1].strip()
        display_name = parts[2].strip()
        stream_format = parts[3].strip() if len(parts) == 4 else "none"
        if command and provider and display_name:
            out.append(
                KnownAgent(
                    command=command,
                    provider=provider,
                    display_name=display_name,
                    stream_format=stream_format,
                )
            )
    return out


def get_all_known_agents() -> list[KnownAgent]:
    """Return built-in agents + any user-defined extras."""
    extra_value = os.environ.get(_EXTRA_ENV_VAR, "")
    return list(KNOWN_AGENTS) + _parse_extra(extra_value)


__all__ = ["KnownAgent", "KNOWN_AGENTS", "get_all_known_agents"]