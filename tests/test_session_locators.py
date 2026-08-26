"""Tests for ``SessionLocator`` and its builder helpers.

Phase 5: provider-agnostic disk fallback. The locator describes the
provider's session-file layout; ``_disk_fallback`` walks it without
knowing which provider it's for.
"""

from __future__ import annotations

from pathlib import Path

from agt.runtime.session_locators import (
    SessionLocator,
    claude_session_locator,
    codex_session_locator,
)


def test_claude_locator_targets_projects_root():
    """Claude locator points at ``~/.claude/projects`` and uses file stem."""
    loc = claude_session_locator()
    assert loc.roots == (Path.home() / ".claude" / "projects",)
    assert loc.glob == "*.jsonl"
    # id_extractor returns the file stem (the session uuid).
    assert loc.id_extractor(Path("/x/y/abc-123.jsonl")) == "abc-123"


def test_codex_locator_targets_both_roots_and_extracts_uuid():
    """Codex locator covers both live + archived sessions and strips the rollout prefix."""
    loc = codex_session_locator()
    assert Path.home() / ".codex" / "sessions" in loc.roots
    assert Path.home() / ".codex" / "archived_sessions" in loc.roots
    # rollout-<ts>-<uuid>.jsonl → last hyphen component (the uuid).
    assert loc.id_extractor(Path("/rollout-2026-01-01T00-abc-def-uuid.jsonl")) == "uuid"


def test_disk_fallback_returns_none_when_locator_is_none():
    """``None`` locator is the documented "no fallback" signal."""
    from agt.task.executor import _disk_fallback

    assert _disk_fallback(None) is None


def test_disk_fallback_picks_most_recent_file(tmp_path):
    """When locator finds multiple candidates, ``_disk_fallback`` returns the latest."""
    from agt.task.executor import _disk_fallback

    root = tmp_path / "projects"
    root.mkdir()
    (root / "older.jsonl").write_text("{}")
    (root / "newer.jsonl").write_text("{}")
    # Force the older one to have an older mtime.
    import os
    import time

    os.utime(root / "older.jsonl", (time.time() - 100, time.time() - 100))
    os.utime(root / "newer.jsonl", (time.time(), time.time()))

    loc = SessionLocator(roots=(root,), glob="*.jsonl")
    assert _disk_fallback(loc) == "newer"


def test_disk_fallback_handles_missing_root(tmp_path):
    """Missing roots are skipped silently; no exception is raised."""
    from agt.task.executor import _disk_fallback

    loc = SessionLocator(
        roots=(tmp_path / "does-not-exist",),
        glob="*.jsonl",
    )
    assert _disk_fallback(loc) is None