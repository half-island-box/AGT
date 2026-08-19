"""Task data models — Task (input) and TaskResult (output)."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path


def _new_task_id() -> str:
    return str(uuid.uuid4())[:8]


@dataclass
class Task:
    """A single execution request handed to an Agent CLI subprocess."""

    prompt: str
    provider: str
    # AGT metadata directory: where AGT writes session.json / meta.json /
    # claim state. Derived from the user's cwd at submit time and persisted
    # to ``tasks.work_dir`` so chat resume stays consistent.
    work_dir: Path | None = None
    # Directory the agent subprocess runs in (its ``cwd``). Defaults to the
    # user's cwd at submit time. Not persisted — when missing, the executor
    # falls back to ``work_dir`` for backward compatibility.
    agent_cwd: Path | None = None
    task_id: str = field(default_factory=_new_task_id)

    # ── Session-related fields ─────────────────────────────────
    # Set by Daemon (not by users) just before delegation to TaskExecutor.
    resume_session_id: str | None = None

    # ── Phase 3 placeholder ────────────────────────────────────
    # Multi-agent collaboration: a task can declare itself a child of another
    # task. The orchestrator in Phase 3 reads this to assemble sub-task DAGs.
    # Phase 2 simply persists it.
    parent_task_id: str | None = None


@dataclass
class TaskResult:
    """The outcome of running a Task through an Agent CLI subprocess."""

    task_id: str
    provider: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float  # seconds
    status: str  # "completed" | "failed" | "timeout" | "cancelled"

    # ── Session-related fields ─────────────────────────────────
    session_id: str | None = None  # Session id produced/used by this run
    resume_rejected: bool = False  # True if resume was attempted but refused


__all__ = ["Task", "TaskResult"]