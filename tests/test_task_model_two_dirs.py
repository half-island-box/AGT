"""Tests for the Task dataclass's two-directory contract.

``work_dir`` = where AGT writes session.json / meta.json / claim state.
``agent_cwd`` = the directory the agent subprocess runs in (its ``cwd``).

The fields are independent. ``Daemon.run`` falls back when one is missing
(legacy single-path mode), and the executor uses ``agent_cwd or work_dir``
as the subprocess cwd.
"""

from __future__ import annotations

from pathlib import Path

from agt.task.models import Task


def test_both_fields_independent() -> None:
    user_cwd = Path("/home/u/project")
    meta = Path("/home/u/project/.agt/tasks/abc")
    t = Task(prompt="x", provider="claude", work_dir=meta, agent_cwd=user_cwd)
    assert t.work_dir == meta
    assert t.agent_cwd == user_cwd
    # Neither field bleeds into the other
    assert t.work_dir != t.agent_cwd


def test_default_both_none() -> None:
    """Default dataclass state — both unset (legacy fallback territory)."""
    t = Task(prompt="x", provider="claude")
    assert t.work_dir is None
    assert t.agent_cwd is None


def test_only_work_dir_set() -> None:
    """Legacy single-path: meta dir doubles as agent cwd."""
    meta = Path("/var/agt/ws/claude/abc")
    t = Task(prompt="x", provider="claude", work_dir=meta)
    assert t.work_dir == meta
    assert t.agent_cwd is None


def test_only_agent_cwd_set() -> None:
    """A user might pin the agent cwd without supplying a meta dir; the
    daemon should still pick a sensible legacy fallback for meta."""
    cwd = Path("/home/u/project")
    t = Task(prompt="x", provider="claude", agent_cwd=cwd)
    assert t.work_dir is None
    assert t.agent_cwd == cwd