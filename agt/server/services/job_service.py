"""Job lifecycle service.

A Job is the long-running owner of a multi-step task. Each Step is an
immutable ``TaskModel`` row. The Job state machine is the single entry
point for status transitions so the rest of the codebase can't leave a
Job in an inconsistent state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import Message, message_from_prompt
from ..models import JobModel, SessionModel, TaskModel

_VALID_STATUSES = {"queued", "running", "waiting_input", "done", "failed", "cancelled"}
_TERMINAL_STATUSES = {"done", "failed", "cancelled"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _new_id() -> str:
    return str(uuid.uuid4())[:8]


async def get_job(db: AsyncSession, job_id: str) -> JobModel | None:
    return await db.get(JobModel, job_id)


async def create_job(
    db: AsyncSession,
    *,
    provider: str,
    prompt: str,
    target_daemon_id: str | None = None,
    work_dir: str | None = None,
    agent_cwd: str | None = None,
    identity_did: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    job_type: str = "task",
    message: Message | None = None,
) -> JobModel:
    """Create a Job and its first pending Step.

    ``message`` is the structured (A2A-shaped) input; when omitted the
    plain ``prompt`` is normalized into a single-TextPart Message so every
    new Step row carries a canonical ``input_message``.
    """
    now = _now()
    j_id = job_id or _new_id()
    s_id = task_id or _new_id()

    job = JobModel(
        job_id=j_id,
        status="queued",
        job_type=job_type,
        provider=provider,
        identity_did=identity_did,
        target_daemon_id=target_daemon_id,
        work_dir=work_dir,
        agent_cwd=agent_cwd,
        created_at=now,
        updated_at=now,
    )
    db.add(job)

    step = TaskModel(
        task_id=s_id,
        job_id=j_id,
        provider=provider,
        prompt=prompt,
        status="pending",
        target_daemon_id=target_daemon_id,
        work_dir=work_dir,
        agent_cwd=agent_cwd,
        identity_did=identity_did,
        input_message=(message or message_from_prompt(prompt)).model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    db.add(step)
    # Wire the Step into the Job's in-memory collection so callers can read
    # ``job.steps`` without an async lazy-load round-trip.
    job.steps.append(step)
    await db.flush()
    return job


async def append_step(
    db: AsyncSession,
    job_id: str,
    prompt: str,
    *,
    resume_session_id: str | None = None,
    message: Message | None = None,
) -> TaskModel:
    """Append a new pending Step to a Job.

    The Job MUST be in a state that accepts new Steps (queued, running, or
    waiting_input). The new Step inherits its runtime metadata from the Job.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})
    if job.status not in {"queued", "running", "waiting_input"}:
        raise ValueError({
            "type": "job_not_accepting_input",
            "message": f"job {job_id} is {job.status}",
        })

    now = _now()
    step = TaskModel(
        task_id=_new_id(),
        job_id=job_id,
        provider=job.provider,
        prompt=prompt,
        status="pending",
        target_daemon_id=job.target_daemon_id,
        session_id=resume_session_id,
        work_dir=job.work_dir,
        agent_cwd=job.agent_cwd,
        identity_did=job.identity_did,
        input_message=(message or message_from_prompt(prompt)).model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    db.add(step)
    await db.flush()

    # Moving from waiting_input back to running means a new Step is ready.
    if job.status == "waiting_input":
        await transition_job(db, job_id, "input_received")

    return step


async def claim_step(db: AsyncSession, task_id: str) -> JobModel:
    """Mark a Step running and move the Job from queued -> running if needed."""
    step = await db.get(TaskModel, task_id)
    if step is None or step.job_id is None:
        raise ValueError({"type": "step_not_found", "message": task_id})
    return await transition_job(db, step.job_id, "step_claimed")


async def report_step(
    db: AsyncSession,
    task_id: str,
    outcome: str | None,
) -> JobModel:
    """Process a terminal Step report and drive the Job state machine.

    ``outcome`` is one of: done, needs_input, error, spawn_subjob. When None,
    the caller is responsible for deriving it before calling this function.
    """
    step = await db.get(TaskModel, task_id)
    if step is None or step.job_id is None:
        raise ValueError({"type": "step_not_found", "message": task_id})

    event = _outcome_to_event(outcome)
    return await transition_job(db, step.job_id, event)


def _outcome_to_event(outcome: str | None) -> str:
    if outcome == "needs_input":
        return "step_needs_input"
    if outcome == "spawn_subjob":
        return "step_spawned"
    if outcome == "error":
        return "step_failed"
    # Default terminal event: done.
    return "step_done"


async def transition_job(db: AsyncSession, job_id: str, event: str) -> JobModel:
    """Idempotent Job state machine.

    Events:
    - step_claimed: queued -> running
    - step_done: running -> done
    - step_needs_input: running -> waiting_input
    - step_failed: running -> failed
    - step_spawned: running -> waiting_children (reserved for future phases)
    - input_received: waiting_input -> running
    - cancel: any non-terminal -> cancelled

    Repeated or invalid events are ignored (returns the Job unchanged).
    """
    job = await db.get(JobModel, job_id)
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})

    old_status = job.status
    new_status = _next_status(old_status, event)

    if new_status != old_status:
        job.status = new_status
        job.updated_at = _now()
        await db.flush()

    return job


def _next_status(current: str, event: str) -> str:
    if current in _TERMINAL_STATUSES:
        return current

    if event == "cancel":
        return "cancelled"

    transitions = {
        "queued": {"step_claimed": "running", "cancel": "cancelled"},
        "running": {
            "step_done": "done",
            "step_needs_input": "waiting_input",
            "step_failed": "failed",
            "step_spawned": "waiting_children",
            "cancel": "cancelled",
        },
        "waiting_input": {"input_received": "running", "cancel": "cancelled"},
        "waiting_children": {"children_done": "running", "cancel": "cancelled"},
    }
    return transitions.get(current, {}).get(event, current)


async def cancel_job(db: AsyncSession, job_id: str) -> JobModel:
    """Cancel a Job and cascade to any running Steps."""
    job = await get_job(db, job_id)
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})

    for step in job.steps:
        if step.status == "running":
            step.status = "cancelled"
            step.updated_at = _now()

    return await transition_job(db, job_id, "cancel")


async def resolve_job_by_session(
    db: AsyncSession,
    session_id: str,
    provider: str | None = None,
    identity_did: str | None = None,
) -> str | None:
    """Return the job_id associated with a session_id.

    When ``provider`` is omitted, any matching session resolves. When
    ``identity_did`` is omitted, the identity-less slot ("") is tried
    alongside the provided value.
    """
    ctxs = {identity_did or ""}
    if identity_did is None:
        ctxs.add("")

    stmt = select(SessionModel).where(
        SessionModel.session_id == session_id,
        SessionModel.identity_did.in_(list(ctxs)),
    )
    if provider is not None:
        stmt = stmt.where(SessionModel.provider == provider)
    stmt = stmt.order_by(SessionModel.updated_at.desc())

    rec = (await db.execute(stmt)).scalars().first()
    return rec.job_id if rec else None


__all__ = [
    "get_job",
    "create_job",
    "append_step",
    "claim_step",
    "report_step",
    "transition_job",
    "cancel_job",
    "resolve_job_by_session",
]
