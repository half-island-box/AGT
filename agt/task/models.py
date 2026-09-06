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
    # Optional AGT metadata directory for the task. Bare ``agt chat``
    # leaves this ``None`` (no per-task subdir is created). Set only when
    # the caller explicitly asked for one (``--work-dir`` override,
    # ``--no-work-dir`` legacy path, or a server-supplied value).
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

    # ── Phase 4 Step 4: identity-driven inject+reclaim ─────────
    # When set, ``TaskExecutor`` injects the identity's AGENTS.md into
    # ``work_dir`` as ``IDENTITY.md`` + a marker block in AGENTS.md
    # before spawn, and reclaims it in ``finally`` after the spawn
    # completes. ``None`` means no identity — legacy behavior.
    identity_did: str | None = None
    # Option D (reverse-dial aware): server-rendered AGENTS.md body for
    # ``identity_did``. ``TaskExecutor`` writes this verbatim into
    # ``work_dir/IDENTITY.md`` instead of copy-from-``Path.home()`` —
    # the daemon doesn't need to fetch the file because the server
    # bundled it into the claim payload. ``None`` falls back to the
    # legacy ``Path.home()/.agt/identities/<slug>/AGENTS.md`` read, so
    # a daemon running outside the server path still works.
    identity_md: str | None = None
    # Phase 3: server-computed artifact whitelist subdirectory (relative
    # to agent_cwd). None = root job, shared ``.agt/outputs/`` (Phase 2
    # behavior). The daemon consumes this verbatim (design D9).
    outputs_subdir: str | None = None


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
    # Phase 2: declared deliverables scanned from ``.agt/outputs/``
    # post-exit. Empty for legacy behavior (no scan / no outputs dir
    # content). Registration is a server-side concern; local runs simply
    # carry the list (the CLI may surface where the files live).
    artifacts: list = field(default_factory=list)
    # Phase 3: structured terminal outcome. Set only when the agent
    # expressed an explicit intent (spawn markers); None leaves the
    # server's legacy derivation in charge.
    outcome: str | None = None
    # Phase 3: outcome sidecar (e.g. {"children": [...]} envelopes).
    outcome_payload: dict | None = None


__all__ = ["Task", "TaskResult"]
