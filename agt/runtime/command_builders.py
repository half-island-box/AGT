"""CommandBuilder — provider-specific agent CLI command construction.

Phase 5: Provider adapter layer. Each provider registers one
``CommandBuilder``; the executor calls ``build(binary, task, stream_args)``
without knowing any provider name. ``stream_args`` is the list of
streaming-specific flags decided by the executor (e.g. claude's
``["--output-format", "stream-json", "--verbose"]``).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from ..task.models import Task


class CommandBuilder(ABC):
    """Build the ``argv`` for spawning one provider's agent CLI subprocess."""

    @abstractmethod
    def build(
        self,
        binary: Path,
        task: Task,
        stream_args: list[str],
    ) -> list[str]: ...


class ClaudeCommandBuilder(CommandBuilder):
    """Build ``<bin> [stream_args...] [--resume <sid>] -p <prompt>``."""

    def build(
        self,
        binary: Path,
        task: Task,
        stream_args: list[str],
    ) -> list[str]:
        cmd: list[str] = [str(binary), *stream_args]
        if task.resume_session_id:
            cmd += ["--resume", task.resume_session_id]
        cmd += ["-p", task.prompt]
        return cmd


class CodexCommandBuilder(CommandBuilder):
    """Build ``<bin> exec --skip-git-repo-check [stream_args...] [resume <sid> --all] <prompt>``.

    Phase 5 leaves Codex on the legacy pipe-text path; this builder exists so
    the adapter layer is complete and Phase B can flip a single registry
    entry to enable it without touching the executor.
    """

    def build(
        self,
        binary: Path,
        task: Task,
        stream_args: list[str],
    ) -> list[str]:
        cmd: list[str] = [
            str(binary),
            "exec",
            "--skip-git-repo-check",
            *stream_args,
        ]
        if task.resume_session_id:
            cmd += ["resume", task.resume_session_id, "--all"]
        cmd += [task.prompt]
        return cmd


__all__ = [
    "CommandBuilder",
    "ClaudeCommandBuilder",
    "CodexCommandBuilder",
]