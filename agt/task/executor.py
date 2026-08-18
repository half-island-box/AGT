"""TaskExecutor — spawn an Agent CLI subprocess to run a single Task.

Implements the "resume first, fallback to fresh" strategy:
- If ``task.resume_session_id`` is set, pass ``--resume <id>``.
- Otherwise create a fresh session and extract the new session_id from the
  agent's output.

Detect resume-rejection by scanning stderr for known refusal phrases.
On timeout, kill the process and return ``status="timeout"``.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path

from ..daemon.config import DaemonConfig
from ..runtime.models import Runtime
from ..server.services.auth_service import TaskToken
from .models import Task, TaskResult
from .stream import StreamReader


# Phrases observed in Claude Code output when --resume cannot find the session.
_RESUME_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"no session found", re.IGNORECASE),
    re.compile(r"session not found", re.IGNORECASE),
    re.compile(r"cannot resume", re.IGNORECASE),
    re.compile(r"resume was rejected", re.IGNORECASE),
    re.compile(r"failed to resume", re.IGNORECASE),
)


# Patterns to extract a fresh session id from agent output.
_SESSION_ID_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"session[_\s-]?id[:\s=]+([A-Za-z0-9_-]{6,})", re.IGNORECASE),
    re.compile(r"\b(?:created|new)\s+session[:\s]+([A-Za-z0-9_-]{6,})", re.IGNORECASE),
)


def _newest_jsonl_session(projects_root: Path) -> str | None:
    """Find the most recently modified <uuid>.jsonl under projects_root."""
    candidates: list[tuple[float, Path]] = []
    try:
        for project_dir in projects_root.iterdir():
            if not project_dir.is_dir():
                continue
            for entry in project_dir.iterdir():
                if entry.suffix != ".jsonl" or not entry.stem:
                    continue
                try:
                    candidates.append((entry.stat().st_mtime, entry))
                except OSError:
                    continue
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1].stem


def _detect_resume_rejected(stderr: str, exit_code: int) -> bool:
    """Return True if the agent explicitly refused the resume attempt."""
    if exit_code == 0:
        return False
    return any(p.search(stderr) for p in _RESUME_REJECT_PATTERNS)


def _extract_session_id_from_output(stdout: str) -> str | None:
    """Try to pull a session id out of the agent's stdout."""
    for pattern in _SESSION_ID_REGEXES:
        match = pattern.search(stdout)
        if match:
            return match.group(1)
    return None


def _extract_session_id_from_claude_projects(work_dir: Path | None) -> str | None:
    """Fallback: scan Claude Code's project directory for the newest session file.

    Claude Code stores each session as a ``<uuid>.jsonl`` file inside
    ``~/.claude/projects/<project-hash>/``. We use the most-recently-modified
    ``.jsonl`` as the best-effort answer to "which session did we just create".
    """
    if work_dir is None:
        return None
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return None
    return _newest_jsonl_session(projects_root)


def _extract_session_id(stdout: str, work_dir: Path | None) -> str | None:
    """Extract a fresh session id, first from stdout, then from disk."""
    from_output = _extract_session_id_from_output(stdout)
    if from_output:
        return from_output
    return _extract_session_id_from_claude_projects(work_dir)


class AgentNotFoundError(RuntimeError):
    """The binary file referenced by the Runtime is missing."""


class TaskExecutor:
    """Run a single Task by spawning one agent subprocess."""

    def __init__(self, runtime: Runtime, config: DaemonConfig) -> None:
        self._runtime = runtime
        self._config = config

    # ── Public entry point ──────────────────────────────────────

    async def execute(self, task: Task, *, interactive: bool = False) -> TaskResult:
        """Run one agent subprocess for ``task``.

        Two modes:

        - **Pipe mode** (``interactive=False``, default): stdout/stderr
          are captured so we can extract the agent's session id and
          enforce a timeout. Used by the daemon (no parent TTY).

        - **Interactive mode** (``interactive=True``): the agent inherits
          the parent's stdio so it sees a real TTY. Some agent CLIs
          (e.g. ``codex``) refuse to run otherwise. We give up output
          capture and session-id extraction — the caller can't use
          ``result.session_id`` for resume.
        """
        binary = self._runtime.agent_entry.path
        if not binary.exists():
            raise AgentNotFoundError(
                f"Agent binary not found at {binary}; run `agt probe` to re-scan."
            )

        cmd = self._build_command(binary, task)
        work_dir = self._ensure_work_dir(task)
        env = self._build_env(task)

        start = time.monotonic()
        if interactive:
            return await self._execute_interactive(cmd, work_dir, env, start)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=env,
        )

        reader = StreamReader(
            stdout=process.stdout,
            stderr=process.stderr,
        )

        timeout = self._config.agent_timeout
        try:
            stdout, stderr = await asyncio.wait_for(reader.read_all(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.task_id,
                provider=task.provider,
                exit_code=-1,
                stdout="",
                stderr="",
                duration=duration,
                status="timeout",
            )

        await process.wait()
        duration = time.monotonic() - start
        exit_code = process.returncode if process.returncode is not None else -1

        # Decide the resume outcome based on what actually happened.
        if task.resume_session_id is not None:
            if _detect_resume_rejected(stderr, exit_code):
                session_id: str | None = None
                resume_rejected = True
            else:
                session_id = task.resume_session_id
                resume_rejected = False
        else:
            session_id = _extract_session_id(stdout, work_dir)
            resume_rejected = False

        if exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        return TaskResult(
            task_id=task.task_id,
            provider=task.provider,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            status=status,
            session_id=session_id,
            resume_rejected=resume_rejected,
        )

    # ── Command construction ────────────────────────────────────

    def _build_command(self, binary: Path, task: Task) -> list[str]:
        cmd: list[str] = [str(binary)]
        if task.resume_session_id:
            cmd += ["--resume", task.resume_session_id]
        cmd += ["-p", task.prompt]
        return cmd

    def _build_env(self, task: Task) -> dict[str, str]:
        env = os.environ.copy()
        # Hint to Claude Code (and friends) that no TTY is attached.
        env.setdefault("CLAUDE_CODE_HEADLESS", "1")
        # Pass the daemon's local resource port to spawned agents so they can
        # call back via 127.0.0.1 for repo checkout etc.
        daemon_port = getattr(self._config, "daemon_port", None)
        if daemon_port:
            env.setdefault("AGT_DAEMON_PORT", str(daemon_port))
        # Token for X-Daemon-Local-Token on LocalResourceServer endpoints.
        local_token = getattr(self._config, "local_token", "")
        if local_token:
            env.setdefault("AGT_DAEMON_TOKEN", local_token)
        # Inject a task-scoped HMAC token so the agent subprocess can call
        # back to the Server (e.g. POST /api/agent/callback). The Server
        # validates X-Task-Token + X-Task-Id against the shared secret.
        agent_id = getattr(self._config, "daemon_id", "anonymous")
        workspace_id = str(task.work_dir) if task.work_dir else ""
        secret = getattr(self._config, "server_secret", "")
        if secret:
            token = TaskToken.create(
                agent_id=agent_id,
                task_id=task.task_id,
                workspace_id=workspace_id,
                secret=secret,
            )
            env["AGT_TASK_TOKEN"] = token
            env["AGT_SERVER_URL"] = self._config.server_url
            env["AGT_TASK_ID"] = task.task_id
        return env

    def _ensure_work_dir(self, task: Task) -> Path:
        if task.work_dir is None:
            return Path.cwd()
        task.work_dir.mkdir(parents=True, exist_ok=True)
        return task.work_dir


__all__ = ["TaskExecutor", "AgentNotFoundError"]