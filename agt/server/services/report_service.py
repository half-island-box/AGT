"""Shared service for recording Step execution results.

Used by both:
- HTTP ``POST /api/tasks/{id}/report`` (local daemons / WS-fallback)
- WS ``tasks.report`` RPC over reverse-dial (remote daemons)

Both paths need exactly the same business logic: find the Step by id,
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

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobModel, SessionModel, StepModel
from ..stream_hub import default_hub
from .artifact_service import artifact_to_out, artifacts_for_steps, register_step_artifacts
from .context_service import apply_context_ops
from .spawn_service import children_for_step, spawn_batches_for_step

log = logging.getLogger(__name__)

_TERMINAL_STEP_STATUSES = {"completed", "failed", "timeout", "cancelled"}


async def record_step_result_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    step_id: str,
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
    context_ops: list[dict] | None = None,
) -> dict:
    """Record execution results on a claimed Step.

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
    step = await db.get(StepModel, step_id)
    if step is None:
        raise ValueError({"type": "task_not_found", "message": step_id})
    # ``step.daemon_id`` is None for never-claimed Steps; we treat that
    # the same as a foreign-daemon mismatch (403) — a daemon can't
    # report on a Step it didn't claim.
    if step.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": step_id})

    # Phase 4 (review fix #4/#8): protect a cancelled Step from late
    # reports. A non-cancelled report arriving after the server already
    # cancelled must not resurrect the step — gentle no-op (not 409,
    # which would trigger the daemon's WS→HTTP report retry storm).
    # Cancelled reports stay idempotently accepted.
    if step.status == "cancelled" and status != "cancelled":
        return {
            "recorded": False,
            "reason": "task_cancelled",
            "artifacts": [],
        }

    # A spawn Step is immutable once its first report materialized the
    # batch. Replaying that report after synthesis starts must not move
    # the parent Job back to waiting_children or append a second
    # synthesis Step.
    if (
        step.status in _TERMINAL_STEP_STATUSES
        and await spawn_batches_for_step(db, step_id)
    ):
        return {
            "recorded": False,
            "reason": "already_terminal",
            "artifacts": [],
        }

    context_results = await apply_context_ops(
        db,
        step=step,
        daemon_id=daemon_id,
        ops=context_ops or [],
        report_status=status,
    )
    for result in context_results:
        if result.get("ok"):
            continue
        message = result.get("error", "context operation failed")
        stderr = f"{stderr}\n[agt] context: {message}".strip()

    step.exit_code = exit_code
    step.stdout = stdout
    step.stderr = stderr
    step.duration = duration
    step.status = status
    step.resume_rejected = resume_rejected

    # Derive outcome for legacy daemons. Chat jobs stay in waiting_input
    # after a clean exit so the user can submit the next turn; task Jobs
    # complete on a clean exit. Query the parent Job explicitly to avoid
    # lazy-loading the ORM relationship inside an async session.
    if outcome is None:
        job_type = "task"
        if step.job_id is not None:
            job = await db.get(JobModel, step.job_id)
            if job is not None:
                job_type = job.job_type
        if exit_code == 0 and job_type == "chat":
            outcome = "needs_input"
        else:
            outcome = "done" if exit_code == 0 else "error"
    step.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Phase 3: persist the structured-outcome sidecar (spawn envelopes).
    if outcome_payload is not None:
        import json as _json

        step.outcome_payload = _json.dumps(outcome_payload, ensure_ascii=False)

    # Phase 2: register declared deliverables. Runs after the ownership
    # check above; per-item problems are skipped inside the service so a
    # scan issue can never break the terminal report.
    artifact_ids = await register_step_artifacts(
        db,
        step=step,
        daemon_id=daemon_id,
        items=artifacts or [],
    )

    # Phase 1: session rows are keyed by (job_id, identity_did, provider).
    # ``identity_did`` normalizes to "" for identity-less Steps so the
    # session pointer matches the empty-string sentinel the model uses.
    ctx = step.identity_did or ""
    job_id = step.job_id

    if resume_rejected:
        # Drop the session pointer — the agent refused to resume, so
        # the next claim on this Step should start a fresh session.
        if job_id is not None:
            await db.execute(
                SessionModel.__table__.delete().where(
                    (SessionModel.job_id == job_id)
                    & (SessionModel.identity_did == ctx)
                    & (SessionModel.provider == step.provider)
                )
            )
        step.session_id = None
    elif session_id:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if job_id is None:
            # Should not happen for new Steps, but tolerate legacy rows
            # that weren't backfilled by writing no session pointer.
            step.session_id = session_id
        else:
            job = await db.get(JobModel, job_id)
            existing = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.job_id == job_id,
                        SessionModel.identity_did == ctx,
                        SessionModel.provider == step.provider,
                    )
                )
            ).scalars().first()
            if existing is None:
                db.add(
                    SessionModel(
                        job_id=job_id,
                        identity_did=ctx,
                        provider=step.provider,
                        session_id=session_id,
                        cwd=job.cwd if job is not None else None,
                        created_at=now,
                        updated_at=now,
                    )
                )
            else:
                existing.session_id = session_id
                existing.updated_at = now
                if job is not None:
                    existing.cwd = job.cwd
            step.session_id = session_id

    await db.flush()

    parent_job = await db.get(JobModel, job_id) if job_id is not None else None
    spawned_children = (
        await children_for_step(db, step_id) if job_id is not None else []
    )

    # MCP spawn creates children while the parent Step is still running.
    # Marker spawns are materialized here for legacy daemons. Both paths
    # converge on the same Step outcome and parent state transition.
    if outcome == "spawn_subjob" and not spawned_children:
        marker_children: list[dict] = []
        if isinstance(outcome_payload, dict):
            raw_children = outcome_payload.get("children")
            if isinstance(raw_children, list):
                marker_children = [
                    child
                    for child in raw_children
                    if isinstance(child, dict) and child.get("goal")
                ]
        if parent_job is not None and marker_children:
            from .job_service import spawn_children

            spawned_children = await spawn_children(
                db,
                parent_job=parent_job,
                children=marker_children,
                daemon_id=daemon_id,
                parent_step_id=step_id,
            )
        if not spawned_children:
            outcome = "error"
            message = "spawn request produced no child jobs"
            step.stderr = f"{step.stderr}\n{message}".strip()
    elif spawned_children:
        if outcome == "error":
            # A failed/timed-out parent cannot synthesize. Cancel its
            # descendants locally so late reports are rejected cleanly.
            from .job_service import cancel_job

            for child in spawned_children:
                await cancel_job(db, child.job_id)
            spawned_children = []
        else:
            if outcome == "needs_input":
                log.warning(
                    "Step %s emitted needs_input after spawning children; "
                    "spawn wins",
                    step_id,
                )
            outcome = "spawn_subjob"

    step.outcome = outcome

    # Drive the parent Job state machine.
    if job_id is not None:
        from .job_service import on_child_done, report_step, try_wake_dependents

        await report_step(db, step_id, outcome)

        if outcome == "spawn_subjob" and spawned_children:
            await on_child_done(db, job_id, synthesis_daemon_id=daemon_id)

        # Phase 3 hook B: this Step's Job may itself be a child — its
        # terminal report wakes the parent's wakeup check (synthesis
        # spawns inline when all siblings are terminal).
        current_job = await db.get(JobModel, job_id)
        if current_job is not None and current_job.parent_job_id is not None:
            await on_child_done(
                db,
                current_job.parent_job_id,
                synthesis_daemon_id=daemon_id,
            )

        # Phase 5 hook C (SOP): a terminal Job wakes its dependency-gated
        # downstream. No-op for jobs with no DAG dependents (the common
        # case), so this fires liberally on every terminal report — SOP
        # nodes have no spawn parent_job_id, hooks A/B never touch them;
        # this hook is their ONLY online wake trigger (review G1).
        if current_job is not None and current_job.status in (
            "done", "failed", "cancelled"
        ):
            await try_wake_dependents(db, current_job.job_id)

    await db.commit()

    # Publish the authoritative terminal payload to any SSE subscriber.
    # Read back from ``step`` (not the parameters) so the fields reflect
    # the resume-rejected cleanup (session_id cleared) and are the exact
    # values a fresh ``/status`` read would see.
    default_hub.publish(
        step_id,
        {
            "type": "done",
            "status": step.status,
            "stdout": step.stdout or "",
            "stderr": step.stderr or "",
            "session_id": step.session_id,
            "exit_code": step.exit_code,
            "resume_rejected": step.resume_rejected,
            "outcome": step.outcome,
            "artifacts": await _step_artifacts_out(db, step_id),
        },
    )
    return {"recorded": True, "artifacts": artifact_ids}


async def _step_artifacts_out(db, step_id: str) -> list:
    """Read back one Step's artifacts for the SSE terminal payload.

    Failure-tolerant: the SSE done event must not fail because of an
    artifact read-back problem (an empty list is an acceptable degrade).
    """
    try:
        by_step = await artifacts_for_steps(db, [step_id])
        return [artifact_to_out(r) for r in by_step.get(step_id, [])]
    except Exception:
        return []


async def record_step_progress_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    step_id: str,
    stdout_prefix: str,
) -> dict:
    """Record an incremental stdout prefix on a running Step.

    Mirrors :func:`record_step_result_for_daemon` ownership rules but is
    deliberately narrow: it only overwrites ``StepModel.stdout`` (with the
    cumulative prefix) and bumps ``updated_at``. ``status`` / ``exit_code``
    / ``duration`` / ``session_id`` / ``resume_rejected`` are terminal
    fields and MUST NOT be touched here — they remain the sole property of
    the terminal ``report`` path (a Step that can pass the ownership check
    is already ``running``; see design decision 1).

    Shrink protection: a prefix no longer than the current ``stdout`` is
    skipped (not written, not published). Anomalous daemons that send a
    shorter prefix must not be allowed to shorten stdout and scramble the
    per-subscriber cursors.
    """
    step = await db.get(StepModel, step_id)
    if step is None:
        raise ValueError({"type": "task_not_found", "message": step_id})
    if step.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": step_id})

    current = step.stdout or ""
    if len(stdout_prefix) <= len(current):
        return {"recorded": False}

    step.stdout = stdout_prefix
    step.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.commit()

    default_hub.publish(step_id, {"type": "progress", "stdout_prefix": stdout_prefix})
    return {"recorded": True}


__all__ = ["record_step_result_for_daemon", "record_step_progress_for_daemon"]
