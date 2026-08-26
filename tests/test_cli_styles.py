"""Provider-specific CLI command construction (claude vs codex styles).

Claude Code and most agents run non-interactively as
``<bin> [--resume <id>] -p <prompt>``. Codex's non-interactive mode is the
``exec`` subcommand with a positional prompt — ``<bin> exec
[resume <id>] <prompt>``. These tests pin both shapes via the
``ProviderAdapter`` registry so a regression in either fails loudly.
"""

from __future__ import annotations

from pathlib import Path

from agt.daemon.config import DaemonConfig
from agt.runtime.command_builders import (
    ClaudeCommandBuilder,
    CodexCommandBuilder,
)
from agt.runtime.registry import get_adapter
from agt.task.executor import _extract_session_id_from_output


def _config() -> DaemonConfig:
    cfg = DaemonConfig.__new__(DaemonConfig)
    cfg.agent_timeout = 30
    return cfg


# ── claude style (default) ───────────────────────────────────────


def test_claude_style_fresh_command() -> None:
    cmd = ClaudeCommandBuilder().build(Path("/usr/bin/claude"), _task(), [])
    assert cmd == ["/usr/bin/claude", "-p", "hello world"]


def test_claude_style_resume_command() -> None:
    cmd = ClaudeCommandBuilder().build(
        Path("/usr/bin/claude"), _task(resume_session_id="s-123"), []
    )
    assert cmd == ["/usr/bin/claude", "--resume", "s-123", "-p", "hello world"]


def test_unknown_style_falls_back_to_claude() -> None:
    """A custom agent (AGT_EXTRA_AGENTS) has no registry entry —
    ``get_adapter`` MUST default to the claude-style invocation."""
    adapter = get_adapter("mystery")
    cmd = adapter.command_builder.build(Path("/usr/bin/mystery"), _task(), [])
    assert cmd == ["/usr/bin/mystery", "-p", "hello world"]


# ── codex style ─────────────────────────────────────────────────


def test_codex_style_fresh_command() -> None:
    cmd = CodexCommandBuilder().build(Path("/usr/bin/codex"), _task(), [])
    assert cmd == [
        "/usr/bin/codex", "exec", "--skip-git-repo-check", "hello world",
    ]


def test_codex_style_resume_command() -> None:
    cmd = CodexCommandBuilder().build(
        Path("/usr/bin/codex"), _task(resume_session_id="s-abc-456"), []
    )
    assert cmd == [
        "/usr/bin/codex", "exec", "--skip-git-repo-check",
        "resume", "s-abc-456", "--all", "hello world",
    ]


# ── session-id extraction (legacy pipe-text path) ────────────────


def test_extract_session_id_from_stdout() -> None:
    """The deprecated text-regex helper still matches ``session id: <uuid>``."""
    out = _extract_session_id_from_output(
        "ok\nsession id: 45f0f655-03ed-4b19-8c8f-6a2b4c9d0e11\ndone"
    )
    assert out == "45f0f655-03ed-4b19-8c8f-6a2b4c9d0e11"


def test_extract_session_id_from_stderr() -> None:
    """codex exec prints its session banner on stderr — extraction must
    search both streams (the executor does so at the call site)."""
    out = _extract_session_id_from_output(
        "provider: openai\nsession id: a1b2c3d4-e5f6-7890-abcd-ef1234567890\n"
    )
    assert out == "a1b2c3d4-e5f6-7890-abcd-ef1234567890"


def test_extract_session_id_none_when_absent() -> None:
    out = _extract_session_id_from_output("plain output\nno ids here")
    assert out is None


# ── KnownAgent registry ──────────────────────────────────────────


def test_known_agents_codex_has_codex_style() -> None:
    from agt.probe.known_agents import KNOWN_AGENTS

    by_command = {a.command: a for a in KNOWN_AGENTS}
    assert by_command["codex"].cli_style == "codex"
    assert by_command["claude"].cli_style == "claude"


# ── helpers ────────────────────────────────────────────────────


def _task(resume_session_id: str | None = None):
    # Late import keeps the dependency on Task models local.
    from agt.task.models import Task

    return Task(
        prompt="hello world",
        provider="test",
        task_id="t-style",
        work_dir=None,
        resume_session_id=resume_session_id,
    )