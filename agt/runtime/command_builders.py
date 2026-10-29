"""CommandBuilder — provider-specific agent CLI command construction.

Phase 5: Provider adapter layer. Each provider registers one
``CommandBuilder``; the executor calls ``build(binary, task, cli_args)``
without knowing any provider name. ``cli_args`` is the list of
pre-prompt CLI flags decided by the executor — permission flags (from
``ProviderAdapter.permission_args``) followed by streaming-specific flags
(e.g. claude's ``["--output-format", "stream-json", "--verbose"]``).
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
        cli_args: list[str],
    ) -> list[str]: ...


class ClaudeCommandBuilder(CommandBuilder):
    """Build ``<bin> [cli_args...] [--resume <sid>] -p <prompt>``."""

    def build(
        self,
        binary: Path,
        task: Task,
        cli_args: list[str],
    ) -> list[str]:
        cmd: list[str] = [str(binary), *cli_args]
        if task.resume_session_id:
            cmd += ["--resume", task.resume_session_id]
        cmd += ["-p", task.prompt]
        return cmd


class CodexCommandBuilder(CommandBuilder):
    """Build ``<bin> exec --skip-git-repo-check [cli_args...] [resume <sid> --all] <prompt>``.

    Legacy pipe-text shape, used when ``codex app-server`` is unavailable
    (old CLI, or ``AGT_CODEX_TRANSPORT=exec``).
    """

    def build(
        self,
        binary: Path,
        task: Task,
        cli_args: list[str],
    ) -> list[str]:
        cmd: list[str] = [
            str(binary),
            "exec",
            "--skip-git-repo-check",
            *cli_args,
        ]
        if task.resume_session_id:
            cmd += ["resume", task.resume_session_id, "--all"]
        cmd += [task.prompt]
        return cmd


class CodexAppServerCommandBuilder(CommandBuilder):
    """Build ``<bin> app-server --listen stdio://``.

    The prompt, resume target, and sandbox policy travel through the
    JSON-RPC handshake instead of argv (see
    :class:`agt.runtime.streaming.CodexAppServerSink`), so ``cli_args`` is
    normally empty.
    """

    def build(
        self,
        binary: Path,
        task: Task,
        cli_args: list[str],
    ) -> list[str]:
        return [str(binary), "app-server", "--listen", "stdio://", *cli_args]


__all__ = [
    "CommandBuilder",
    "ClaudeCommandBuilder",
    "CodexCommandBuilder",
    "CodexAppServerCommandBuilder",
]
