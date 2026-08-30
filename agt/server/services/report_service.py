"""Shared service for recording task execution results.

Used by both:
- HTTP ``POST /api/tasks/{id}/report`` (local daemons / WS-fallback)
- WS ``tasks.report`` RPC over reverse-dial (remote daemons)

Both paths need exactly the same business logic: find the task by id,
verify the report comes from the daemon that actually claimed it,
write result fields, and update the session pointer. Keeping this in
one function means the two entry points can't drift on validation or
session handling.

Business errors are raised as ``ValueError(dict_payload)`` so callers
can map them to ``HTTPException`` (HTTP path) or ``RPCRejected`` (WS
path). The dict carries a ``type`` discriminator:

- ``task_not_found``  — no task with that id (404)
- ``task_not_owned``  — task isn't currently owned by ``daemon_id`` (403)

This matches the existing convention used by ``claim_service`` and
``runtime_service`` — no new exception classes introduced.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SessionModel, TaskModel
from ..stream_hub import default_hub


async def record_task_result_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    task_id: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    status: str,
    session_id: str | None,
    resume_rejected: bool,
) -> dict:
    """Record execution results on a claimed task.

    ``daemon_id`` semantics differ by caller:
    - HTTP path passes ``payload.daemon_id`` (backward compat).
    - WS path passes the configured ``self.daemon_id`` (body value
      ignored, so a misbehaving daemon can't spoof another daemon's
      report).

    On success returns ``{"recorded": True}``. On business failure
    raises ``ValueError({"type": "task_not_found"|"task_not_owned",
    ...})``.
    """
    task = await db.get(TaskModel, task_id)
    if task is None:
        raise ValueError({"type": "task_not_found", "message": task_id})
    # ``task.daemon_id`` is None for never-claimed tasks; we treat that
    # the same as a foreign-daeon mismatch (403) — a daemon can't
    # report on a task it didn't claim.
    if task.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": task_id})

    task.exit_code = exit_code
    task.stdout = stdout
    task.stderr = stderr
    task.duration = duration
    task.status = status
    task.resume_rejected = resume_rejected
    task.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Phase 4: session rows are keyed by (task_id, identity_did, provider).
    # ``identity_did`` normalizes to "" for identity-less tasks so the
    # session pointer matches the empty-string sentinel the model uses.
    ctx = task.identity_did or ""

    if resume_rejected:
        # Drop the session pointer — the agent refused to resume, so
        # the next claim on this task should start a fresh session.
        await db.execute(
            SessionModel.__table__.delete().where(
                (SessionModel.task_id == task_id)
                & (SessionModel.identity_did == ctx)
                & (SessionModel.provider == task.provider)
            )
        )
        task.session_id = None
    elif session_id:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        existing = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.task_id == task_id,
                    SessionModel.identity_did == ctx,
                    SessionModel.provider == task.provider,
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(
                SessionModel(
                    task_id=task_id,
                    identity_did=ctx,
                    provider=task.provider,
                    session_id=session_id,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            existing.session_id = session_id
            existing.updated_at = now
        task.session_id = session_id

    await db.commit()

    # Publish the authoritative terminal payload to any SSE subscriber.
    # Read back from ``task`` (not the parameters) so the fields reflect
    # the resume-rejected cleanup (session_id cleared) and are the exact
    # values a fresh ``/status`` read would see.
    default_hub.publish(
        task_id,
        {
            "type": "done",
            "status": task.status,
            "stdout": task.stdout or "",
            "stderr": task.stderr or "",
            "session_id": task.session_id,
            "exit_code": task.exit_code,
            "resume_rejected": task.resume_rejected,
        },
    )
    return {"recorded": True}


async def record_task_progress_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    task_id: str,
    stdout_prefix: str,
) -> dict:
    """Record an incremental stdout prefix on a running task.

    Mirrors :func:`record_task_result_for_daemon` ownership rules but is
    deliberately narrow: it only overwrites ``TaskModel.stdout`` (with the
    cumulative prefix) and bumps ``updated_at``. ``status`` / ``exit_code``
    / ``duration`` / ``session_id`` / ``resume_rejected`` are terminal
    fields and MUST NOT be touched here — they remain the sole property of
    the terminal ``report`` path (a task that can pass the ownership check
    is already ``running``; see design decision 1).

    Shrink protection: a prefix no longer than the current ``stdout`` is
    skipped (not written, not published). Anomalous daemons that send a
    shorter prefix must not be allowed to shorten stdout and scramble the
    per-subscriber cursors.
    """
    task = await db.get(TaskModel, task_id)
    if task is None:
        raise ValueError({"type": "task_not_found", "message": task_id})
    if task.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": task_id})

    current = task.stdout or ""
    if len(stdout_prefix) <= len(current):
        return {"recorded": False}

    task.stdout = stdout_prefix
    task.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.commit()

    default_hub.publish(task_id, {"type": "progress", "stdout_prefix": stdout_prefix})
    return {"recorded": True}


__all__ = ["record_task_result_for_daemon", "record_task_progress_for_daemon"]