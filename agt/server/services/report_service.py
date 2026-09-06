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

from ..models import JobModel, SessionModel, TaskModel
from ..stream_hub import default_hub
from .artifact_service import artifact_to_out, artifacts_for_steps, register_step_artifacts


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
    outcome: str | None = None,
    artifacts: list | None = None,
    outcome_payload: dict | None = None,
) -> dict:
    """Record execution results on a claimed task (Step).

    ``daemon_id`` semantics differ by caller:
    - HTTP path passes ``payload.daemon_id`` (backward compat).
    - WS path passes the configured ``self.daemon_id`` (body value
      ignored, so a misbehaving daemon can't spoof another daemon's
      report).

    Phase 1: ``outcome`` drives the parent Job state machine. Legacy
    daemons that don't send ``outcome`` have it derived from
    ``exit_code`` (0 -> done, otherwise error).

    Phase 2: ``artifacts`` (optional ``ArtifactReportItem`` list) is
    persisted via ``register_step_artifacts`` AFTER the ownership check —
    only the daemon that claimed this Step can register its deliverables.
    Registration failures never fail the report (best-effort by design).

    Phase 3: ``outcome_payload`` persists to the Step's sidecar column,
    then drives two same-transaction hooks (spawn materialization +
    parent wakeup).

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

    # Derive outcome for legacy daemons. Chat jobs stay in waiting_input
    # after a clean exit so the user can submit the next turn; task jobs
    # complete on a clean exit. Query the parent Job explicitly to avoid
    # lazy-loading the ORM relationship inside an async session.
    if outcome is None:
        job_type = "task"
        if task.job_id is not None:
            job = await db.get(JobModel, task.job_id)
            if job is not None:
                job_type = job.job_type
        if exit_code == 0 and job_type == "chat":
            outcome = "needs_input"
        else:
            outcome = "done" if exit_code == 0 else "error"
    task.outcome = outcome

    task.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Phase 3: persist the structured-outcome sidecar (spawn envelopes).
    if outcome_payload is not None:
        import json as _json

        task.outcome_payload = _json.dumps(outcome_payload, ensure_ascii=False)

    # Phase 2: register declared deliverables. Runs after the ownership
    # check above; per-item problems are skipped inside the service so a
    # scan issue can never break the terminal report.
    artifact_ids = await register_step_artifacts(
        db,
        task=task,
        daemon_id=daemon_id,
        items=artifacts or [],
    )

    # Phase 1: session rows are keyed by (job_id, identity_did, provider).
    # ``identity_did`` normalizes to "" for identity-less tasks so the
    # session pointer matches the empty-string sentinel the model uses.
    ctx = task.identity_did or ""
    job_id = task.job_id

    if resume_rejected:
        # Drop the session pointer — the agent refused to resume, so
        # the next claim on this task should start a fresh session.
        if job_id is not None:
            await db.execute(
                SessionModel.__table__.delete().where(
                    (SessionModel.job_id == job_id)
                    & (SessionModel.identity_did == ctx)
                    & (SessionModel.provider == task.provider)
                )
            )
        task.session_id = None
    elif session_id:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if job_id is None:
            # Should not happen for new tasks, but tolerate legacy rows
            # that weren't backfilled by writing no session pointer.
            task.session_id = session_id
        else:
            existing = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.job_id == job_id,
                        SessionModel.identity_did == ctx,
                        SessionModel.provider == task.provider,
                    )
                )
            ).scalars().first()
            if existing is None:
                db.add(
                    SessionModel(
                        job_id=job_id,
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

    await db.flush()

    # Drive the parent Job state machine.
    if job_id is not None:
        from .job_service import report_step

        await report_step(db, task_id, outcome)

        # Phase 3 hook A: materialize spawn intents as parallel child
        # Jobs (same transaction — a failed spawn rolls back with the
        # report). Failed-at-birth children (unknown identity) count as
        # terminal, so run the wakeup check immediately afterwards.
        if outcome == "spawn_subjob":
            from .job_service import on_child_done, spawn_children

            children = []
            if isinstance(outcome_payload, dict):
                raw_children = outcome_payload.get("children")
                if isinstance(raw_children, list):
                    children = [
                        c for c in raw_children if isinstance(c, dict) and c.get("goal")
                    ]
            parent_job = await db.get(JobModel, job_id)
            if parent_job is not None:
                await spawn_children(
                    db,
                    parent_job=parent_job,
                    children=children,
                    daemon_id=daemon_id,
                )
                if children:
                    await on_child_done(db, job_id, synthesis_daemon_id=daemon_id)

        # Phase 3 hook B: this Step's Job may itself be a child — its
        # terminal report wakes the parent's wakeup check (synthesis
        # spawns inline when all siblings are terminal).
        current_job = await db.get(JobModel, job_id)
        if current_job is not None and current_job.parent_job_id is not None:
            from .job_service import on_child_done

            await on_child_done(
                db,
                current_job.parent_job_id,
                synthesis_daemon_id=daemon_id,
            )

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
            "outcome": task.outcome,
            "artifacts": await _step_artifacts_out(db, task_id),
        },
    )
    return {"recorded": True, "artifacts": artifact_ids}


async def _step_artifacts_out(db, task_id: str) -> list:
    """Read back one Step's artifacts for the SSE terminal payload.

    Failure-tolerant: the SSE done event must not fail because of an
    artifact read-back problem (an empty list is an acceptable degrade).
    """
    try:
        by_step = await artifacts_for_steps(db, [task_id])
        return [artifact_to_out(r) for r in by_step.get(task_id, [])]
    except Exception:
        return []


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
