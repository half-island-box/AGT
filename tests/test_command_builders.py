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
        Path("/bin/codex"), _task("say hi"), ["--sandbox", "workspace-write", "--approval-mode", "full-auto"]
    )
    assert cmd == [
        "/bin/codex",
        "exec",
        "--skip-git-repo-check",
        "--sandbox",
        "workspace-write",
        "--approval-mode",
        "full-auto",
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