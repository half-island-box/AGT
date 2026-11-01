"""Tests for ``CommandBuilder`` implementations.

Phase 5: each provider builds the agent CLI argv without leaking
provider-specific strings into the executor. These tests pin the
expected argv shape for claude / codex, including the streaming-args
passthrough (which the executor decides — the builder itself is
unaware of *whether* streaming is active).
"""

from __future__ import annotations

from pathlib import Path

from agt.runtime.command_builders import (
    ClaudeCommandBuilder,
    CodexCommandBuilder,
    OpenCodeCommandBuilder,
)
from agt.task.models import Task


def _task(prompt: str = "hi", resume_session_id: str | None = None) -> Task:
    return Task(prompt=prompt, provider="claude", task_id="t1", resume_session_id=resume_session_id)


def test_claude_builder_basic_prompt():
    """No resume, no streaming: ``<bin> -p <prompt>``."""
    cmd = ClaudeCommandBuilder().build(Path("/bin/claude"), _task("say hi"), [])
    assert cmd == ["/bin/claude", "-p", "say hi"]


def test_claude_builder_with_resume():
    """Resume: ``<bin> --resume <sid> -p <prompt>``."""
    cmd = ClaudeCommandBuilder().build(
        Path("/bin/claude"), _task("say hi", resume_session_id="sess-abc"), []
    )
    assert cmd == ["/bin/claude", "--resume", "sess-abc", "-p", "say hi"]


def test_claude_builder_with_stream_args():
    """Streaming args (decided by the executor) appear before resume/-p."""
    cmd = ClaudeCommandBuilder().build(
        Path("/bin/claude"),
        _task("say hi", resume_session_id="sess-abc"),
        ["--output-format", "stream-json", "--verbose"],
    )
    assert cmd == [
        "/bin/claude",
        "--output-format",
        "stream-json",
        "--verbose",
        "--resume",
        "sess-abc",
        "-p",
        "say hi",
    ]


def test_claude_builder_stream_args_no_resume():
    cmd = ClaudeCommandBuilder().build(
        Path("/bin/claude"), _task("say hi"), ["--output-format", "stream-json", "--verbose"]
    )
    assert cmd == ["/bin/claude", "--output-format", "stream-json", "--verbose", "-p", "say hi"]


def test_codex_builder_basic_prompt():
    """Codex non-streaming: ``<bin> exec --skip-git-repo-check <prompt>``."""
    cmd = CodexCommandBuilder().build(Path("/bin/codex"), _task("say hi"), [])
    assert cmd == ["/bin/codex", "exec", "--skip-git-repo-check", "say hi"]


def test_codex_builder_with_resume():
    """Codex resume: nested subcommand ``resume <sid> --all``."""
    cmd = CodexCommandBuilder().build(
        Path("/bin/codex"), _task("say hi", resume_session_id="sess-abc"), []
    )
    assert cmd == [
        "/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "resume",
        "sess-abc",
        "--all",
        "say hi",
    ]


def test_codex_builder_with_permission_args():
    """Permission args (from the adapter) slot in before resume/-p."""
    cmd = CodexCommandBuilder().build(
        Path("/bin/codex"), _task("say hi"), ["--approve-for-me"]
    )
    assert cmd == [
        "/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--approve-for-me",
        "say hi",
    ]


def test_codex_builder_stream_args_pass_through():
    """Builder is provider-agnostic about stream args — they just slot in."""
    cmd = CodexCommandBuilder().build(
        Path("/bin/codex"),
        _task("say hi"),
        ["--json"],  # what Phase B will pass
    )
    assert cmd == ["/bin/codex", "exec", "--skip-git-repo-check", "--json", "say hi"]


# ── opencode ────────────────────────────────────────────────────


def test_opencode_builder_basic_prompt(monkeypatch):
    """No resume, no stream args: ``<bin> run <prompt>``."""
    monkeypatch.delenv("AGT_OPENCODE_MODEL", raising=False)
    cmd = OpenCodeCommandBuilder().build(Path("/bin/opencode"), _task("say hi"), [])
    assert cmd == ["/bin/opencode", "run", "say hi"]


def test_opencode_builder_model_env(monkeypatch):
    """``AGT_OPENCODE_MODEL`` supplies ``--model`` for headless runs."""
    monkeypatch.setenv("AGT_OPENCODE_MODEL", "deepseek/deepseek-v4-flash")
    cmd = OpenCodeCommandBuilder().build(Path("/bin/opencode"), _task("say hi"), [])
    assert cmd == [
        "/bin/opencode",
        "run",
        "--model",
        "deepseek/deepseek-v4-flash",
        "say hi",
    ]


def test_opencode_builder_with_resume_and_stream_args(monkeypatch):
    """Stream args slot after ``run``; ``--session <sid>`` precedes the prompt."""
    monkeypatch.delenv("AGT_OPENCODE_MODEL", raising=False)
    cmd = OpenCodeCommandBuilder().build(
        Path("/bin/opencode"),
        _task("say hi", resume_session_id="ses_abc"),
        ["--auto", "--standalone", "--format", "json", "--thinking"],
    )
    assert cmd == [
        "/bin/opencode",
        "run",
        "--auto",
        "--standalone",
        "--format",
        "json",
        "--thinking",
        "--session",
        "ses_abc",
        "say hi",
    ]
