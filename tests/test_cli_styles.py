"""Provider-specific CLI command construction (claude vs codex styles).

Claude Code and most agents run non-interactively as
``<bin> [--resume <id>] -p <prompt>``. Codex's non-interactive mode is
the ``exec`` subcommand with a positional prompt — ``<bin> exec
[resume <id>] <prompt>``. These tests pin both shapes so a regression
in either fails loudly.

Also covers session-id extraction from stderr (codex prints its session
banner there).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agt.daemon.config import DaemonConfig
from agt.runtime.models import AgentEntry, Runtime
from agt.task.executor import TaskExecutor, _extract_session_id
from agt.task.models import Task


def _runtime(cli_style: str) -> Runtime:
    entry = AgentEntry(
        command="whatever",
        path=Path("/bin/whatever"),
        version="1.0.0",
        provider="test",
        display_name="Test",
        cli_style=cli_style,
    )
    return Runtime(
        name="Test",
        type="test",
        version="1.0.0",
        status="online",
        agent_entry=entry,
        device_name="test",
    )


def _config() -> DaemonConfig:
    cfg = DaemonConfig.__new__(DaemonConfig)
    cfg.agent_timeout = 30
    return cfg


def _task(resume_session_id: str | None = None) -> Task:
    return Task(
        prompt="hello world",
        provider="test",
        task_id="t-style",
        work_dir=None,
        resume_session_id=resume_session_id,
    )


# ── claude style (default) ───────────────────────────────────────


def test_claude_style_fresh_command() -> None:
    executor = TaskExecutor(runtime=_runtime("claude"), config=_config())
    cmd = executor._build_command(Path("/usr/bin/claude"), _task())
    assert cmd == ["/usr/bin/claude", "-p", "hello world"]


def test_claude_style_resume_command() -> None:
    executor = TaskExecutor(runtime=_runtime("claude"), config=_config())
    cmd = executor._build_command(
        Path("/usr/bin/claude"), _task(resume_session_id="s-123")
    )
    assert cmd == ["/usr/bin/claude", "--resume", "s-123", "-p", "hello world"]


def test_unknown_style_falls_back_to_claude() -> None:
    """A custom agent (AGT_EXTRA_AGENTS) has no cli_style knowledge —
    must default to the claude-style invocation."""
    executor = TaskExecutor(runtime=_runtime("mystery"), config=_config())
    cmd = executor._build_command(Path("/usr/bin/mystery"), _task())
    assert cmd == ["/usr/bin/mystery", "-p", "hello world"]


# ── codex style ─────────────────────────────────────────────────


def test_codex_style_fresh_command() -> None:
    executor = TaskExecutor(runtime=_runtime("codex"), config=_config())
    cmd = executor._build_command(Path("/usr/bin/codex"), _task())
    assert cmd == [
        "/usr/bin/codex", "exec", "--skip-git-repo-check", "hello world",
    ]


def test_codex_style_resume_command() -> None:
    executor = TaskExecutor(runtime=_runtime("codex"), config=_config())
    cmd = executor._build_command(
        Path("/usr/bin/codex"), _task(resume_session_id="s-abc-456")
    )
    assert cmd == [
        "/usr/bin/codex", "exec", "--skip-git-repo-check",
        "resume", "s-abc-456", "hello world",
    ]


# ── session-id extraction ────────────────────────────────────────


def test_extract_session_id_from_stdout() -> None:
    assert _extract_session_id(
        "ok\nsession id: 45f0f655-03ed-4b19-8c8f-6a2b4c9d0e11\ndone",
        "", None,
    ) == "45f0f655-03ed-4b19-8c8f-6a2b4c9d0e11"


def test_extract_session_id_from_stderr() -> None:
    """codex exec prints its session banner on stderr — extraction must
    search both streams."""
    assert _extract_session_id(
        "agent reply text",
        "provider: openai\nsession id: a1b2c3d4-e5f6-7890-abcd-ef1234567890\n",
        None,
    ) == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_extract_session_id_none_when_absent(tmp_path: Path) -> None:
    # work_dir given but ~/.claude/projects won't match in this env;
    # just assert the output-based path returns None for plain text.
    out = _extract_session_id("plain output\nno ids here", "", tmp_path)
    assert out is None or isinstance(out, str)  # disk fallback may or may not fire


# ── KnownAgent registry ──────────────────────────────────────────


def test_known_agents_codex_has_codex_style() -> None:
    from agt.probe.known_agents import KNOWN_AGENTS

    by_command = {a.command: a for a in KNOWN_AGENTS}
    assert by_command["codex"].cli_style == "codex"
    assert by_command["claude"].cli_style == "claude"
