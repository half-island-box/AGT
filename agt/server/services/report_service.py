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

    if resume_rejected:
        # Drop the session pointer — the agent refused to resume, so
        # the next claim on this task should start a fresh session.
        await db.execute(
            SessionModel.__table__.delete().where(
                (SessionModel.task_id == task_id)
                & (SessionModel.provider == task.provider)
            )
        )
        task.session_id = None
    elif session_id:
        existing = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.task_id == task_id,
                    SessionModel.provider == task.provider,
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(
                SessionModel(
                    task_id=task_id,
                    provider=task.provider,
                    session_id=session_id,
                    created_at=datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    ),
                )
            )
        else:
            existing.session_id = session_id
        task.session_id = session_id

    await db.commit()
    return {"recorded": True}


__all__ = ["record_task_result_for_daemon"]