"""SessionLocator — describe where a provider writes session files.

Phase 5: provider adapter layer. Each provider registers a
``SessionLocator`` describing how to discover a fresh ``session_id`` from
the local filesystem as a fallback when the stream sink cannot extract one
from the structured event payload.

The executor's ``_disk_fallback`` helper takes a locator and walks
``roots`` recursively, applying ``glob`` and ``id_extractor`` to find the
most-recently-modified session file.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SessionLocator:
    """How to find this provider's session files on disk."""

    roots: tuple[Path, ...]
    glob: str = "*.jsonl"
    id_extractor: Callable[[Path], str | None] = lambda p: p.stem


def claude_session_locator() -> SessionLocator:
    """Build a ``SessionLocator`` for Claude Code transcripts.

    Each Claude Code session lives at
    ``~/.claude/projects/<project-hash>/<session-uuid>.jsonl``; the
    ``session_id`` is the file stem.
    """

    return SessionLocator(
        roots=(Path.home() / ".claude" / "projects",),
        glob="*.jsonl",
        id_extractor=lambda p: p.stem,
    )


def codex_session_locator() -> SessionLocator:
    """Build a ``SessionLocator`` for Codex CLI rollouts.

    Codex writes one ``rollout-<ts>-<uuid>.jsonl`` per session under
    ``~/.codex/sessions/<YYYY>/<MM>/<DD>/`` (and ``archived_sessions/``);
    the ``session_id`` is the last hyphen-separated component of the file
    stem.
    """

    return SessionLocator(
        roots=(
            Path.home() / ".codex" / "sessions",
            Path.home() / ".codex" / "archived_sessions",
        ),
        glob="*.jsonl",
        id_extractor=lambda p: p.stem.rsplit("-", 1)[-1],
    )


__all__ = [
    "SessionLocator",
    "claude_session_locator",
    "codex_session_locator",
]