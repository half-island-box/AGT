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

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobModel, SessionModel, StepEventModel, StepModel
from ..stream_hub import default_hub
from .artifact_service import artifact_to_out, artifacts_for_steps, register_step_artifacts
from .context_service import apply_context_ops
from .spawn_service import children_for_step, spawn_batches_for_step

log = logging.getLogger(__name__)

_TERMINAL_STEP_STATUSES = {"completed", "failed", "timeout", "cancelled"}


# ── Structured agent events ─────────────────────────────────────────
#
# Events are append-only rows keyed by ``(task_id, seq)``. Every write path
# (progress batches and the report tail) goes through these helpers so the
# idempotency rule lives in one place.


def normalize_agent_events(raw) -> list[dict]:
    """Validate, dedupe (by ``seq``), and sort one batch of events.

    Malformed entries are dropped — the transport must never fail because
    of one bad event. Accepts pydantic models (HTTP path) or plain dicts
    (WS path). Returns ``{seq, kind, text_offset, payload}`` records.
    """
    if not raw:
        return []
    seen: set[int] = set()
    out: list[dict] = []
    for item in raw:
        if hasattr(item, "model_dump"):
            item = item.model_dump()
        if not isinstance(item, dict):
            continue
        seq = item.get("seq")
        kind = item.get("kind")
        if isinstance(seq, bool) or not isinstance(seq, int) or seq < 0:
            continue
        if not isinstance(kind, str) or not kind:
            continue
        if seq in seen:
            continue
        seen.add(seq)
        offset = item.get("text_offset", 0)
        if isinstance(offset, bool) or not isinstance(offset, int):
            offset = 0
        payload = {
            key: value
            for key, value in item.items()
            if key not in ("seq", "kind", "text_offset")
        }
        out.append(
            {"seq": seq, "kind": kind, "text_offset": offset, "payload": payload}
        )
    out.sort(key=lambda event: event["seq"])
    return out


async def _insert_agent_events(
    db: AsyncSession, step_id: str, events: list[dict]
) -> list[dict]:
    """Insert new events idempotently; return only the actually-new ones.

    A re-sent batch (progress retry, report tail overlapping an
    acknowledged batch) must neither duplicate rows nor be re-published to
    SSE subscribers, so existing ``seq`` values are filtered out first.
    ``INSERT OR IGNORE`` remains as a race guard.
    """
    if not events:
        return []
    existing = set(
        (
            await db.execute(
                select(StepEventModel.seq).where(
                    StepEventModel.step_id == step_id,
                    StepEventModel.seq.in_([event["seq"] for event in events]),
                )
            )
        ).scalars().all()
    )
    fresh = [event for event in events if event["seq"] not in existing]
    if not fresh:
        return []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    rows = [
        {
            "task_id": step_id,
            "seq": event["seq"],
            "kind": event["kind"],
            "text_offset": event["text_offset"],
            "payload_json": json.dumps(event["payload"], ensure_ascii=False),
            "created_at": now,
        }
        for event in fresh
    ]
    stmt = sqlite_insert(StepEventModel).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["task_id", "seq"])
    await db.execute(stmt)
    return [event_to_wire(event) for event in fresh]


def event_to_wire(event: dict) -> dict:
    """Flatten a ``{seq, kind, text_offset, payload}`` record for the wire."""
    return {
        "seq": event["seq"],
        "kind": event["kind"],
        "text_offset": event["text_offset"],
        **event["payload"],
    }


def event_row_to_wire(row: StepEventModel) -> dict:
    """Reconstruct the wire shape from a stored ``step_events`` row."""
    try:
        payload = json.loads(row.payload_json or "{}")
    except (TypeError, ValueError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {"value": payload}
    return {
        "seq": row.seq,
        "kind": row.kind,
        "text_offset": row.text_offset or 0,
        **payload,
    }


def usage_to_out(usage) -> dict | None:
    """Normalize a usage dict/model into the wire/DB shape.

    Accepts a ``UsageOut`` model (HTTP), a plain dict (WS), or ``None``.
    Returns ``None`` when nothing usable was supplied.
    """
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        usage = usage.model_dump()
    if not isinstance(usage, dict):
        return None
    out: dict = {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    for key in ("cache_read_input_tokens", "cache_creation_input_tokens"):
        value = usage.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            out[key] = value
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        out["cost_usd"] = float(cost)
    turns = usage.get("num_turns")
    if isinstance(turns, int) and not isinstance(turns, bool):
        out["num_turns"] = turns
    model = usage.get("model")
    if isinstance(model, str) and model:
        out["model"] = model
    return out or None


def _apply_usage(step: StepModel, usage) -> None:
    """Write the turn-level usage summary onto the Step columns."""
    data = usage_to_out(usage)
    if not data:
        return
    step.input_tokens = data.get("input_tokens")
    step.output_tokens = data.get("output_tokens")
    step.cache_read_tokens = data.get("cache_read_input_tokens")
    step.cache_creation_tokens = data.get("cache_creation_input_tokens")
    step.cost_usd = data.get("cost_usd")
    step.model = data.get("model")
    step.num_turns = data.get("num_turns")


def step_usage_out(step: StepModel) -> dict | None:
    """Build the wire usage summary from a Step row (``None`` if empty)."""
    fields = {
        "input_tokens": step.input_tokens,
        "output_tokens": step.output_tokens,
        "cache_read_input_tokens": step.cache_read_tokens,
        "cache_creation_input_tokens": step.cache_creation_tokens,
        "cost_usd": step.cost_usd,
        "model": step.model,
        "num_turns": step.num_turns,
    }
    if all(value is None for value in fields.values()):
        return None
    return fields


async def last_event_seq(db: AsyncSession, step_id: str) -> int:
    """Highest stored ``seq`` for a Step (0 when it has no events)."""
    value = await db.scalar(
        select(func.max(StepEventModel.seq)).where(
            StepEventModel.step_id == step_id
        )
    )
    return int(value or 0)


async def events_for_step(db: AsyncSession, step_id: str) -> list[dict]:
    """All stored events for a Step in ``seq`` order, in wire shape."""
    rows = (
        await db.execute(
            select(StepEventModel)
            .where(StepEventModel.step_id == step_id)
            .order_by(StepEventModel.seq)
        )
    ).scalars().all()
    return [event_row_to_wire(row) for row in rows]


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
    session_title: str | None = None,
    outcome: str | None = None,
    artifacts: list | None = None,
    outcome_payload: dict | None = None,
    context_ops: list[dict] | None = None,
    usage: dict | None = None,
    agent_events: list | None = None,
) -> dict:
    """Record execution results on a claimed Step.
    """
    step = await db.get(StepModel, step_id)
    if step is None:
        raise ValueError({"type": "task_not_found", "message": step_id})

    if step.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": step_id})


    # A Server-side interruption is authoritative. Once the Step is
    # cancelled, a daemon report is only a late transport acknowledgement:
    # it must not mutate the interrupted transcript, publish another terminal
    # frame, or drive the Job state machine while a follow-up turn runs. A
    # cancellation report may still reveal the provider session pointer, so
    # preserve (or drop) only that pointer for a later turn.
    if step.status == "cancelled":
        if status == "cancelled" and step.job_id is not None:
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            parent_job = await db.get(JobModel, step.job_id)
            if (
                session_title
                and parent_job is not None
                and parent_job.job_type == "chat"
            ):
                parent_job.title = session_title
            existing = (
                await db.execute(
                    select(SessionModel).where(
                        SessionModel.job_id == step.job_id,
                        SessionModel.identity_did == (step.identity_did or ""),
                        SessionModel.provider == step.provider,
                    )
                )
            ).scalars().first()
            if resume_rejected:
                if existing is not None:
                    await db.delete(existing)
                step.session_id = None
            elif session_id:
                if existing is None:
                    db.add(
                        SessionModel(
                            job_id=step.job_id,
                            identity_did=step.identity_did or "",
                            provider=step.provider,
                            session_id=session_id,
                            cwd=parent_job.cwd if parent_job is not None else None,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                else:
                    existing.session_id = session_id
                    existing.updated_at = now
                    if parent_job is not None:
                        existing.cwd = parent_job.cwd
                step.session_id = session_id
            await db.commit()
        return {
            "recorded": False,
            "reason": "task_cancelled",
            "artifacts": [],
        }


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

    _apply_usage(step, usage)


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


    new_events = await _insert_agent_events(
        db, step_id, normalize_agent_events(agent_events)
    )

    ctx = step.identity_did or ""
    job_id = step.job_id
    title_job = await db.get(JobModel, job_id) if job_id is not None else None
    if (
        session_title
        and title_job is not None
        and title_job.job_type == "chat"
    ):
        title_job.title = session_title

    if resume_rejected:
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

        current_job = await db.get(JobModel, job_id)
        if current_job is not None and current_job.parent_job_id is not None:
            await on_child_done(
                db,
                current_job.parent_job_id,
                synthesis_daemon_id=daemon_id,
            )
        if current_job is not None and current_job.status in (
            "done", "failed", "cancelled"
        ):
            await try_wake_dependents(db, current_job.job_id)

    await db.commit()

    # Publish the authoritative terminal payload to any SSE subscriber.
    if new_events:
        default_hub.publish(
            step_id, {"type": "agent_events", "events": new_events}
        )
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
            "usage": step_usage_out(step),
            "model": step.model,
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
    events: list | None = None,
) -> dict:
    """Record an incremental stdout prefix and/or event batch on a Step.

    Mirrors :func:`record_step_result_for_daemon` ownership rules but is
    deliberately narrow: it only overwrites ``StepModel.stdout`` (with the
    cumulative prefix) and appends ``step_events`` rows. ``status`` /
    ``exit_code`` / ``duration`` / ``session_id`` / ``resume_rejected`` are
    terminal fields and MUST NOT be touched here — they remain the sole
    property of the terminal ``report`` path (a Step that can pass the
    ownership check is already ``running``).

    Shrink protection: a prefix no longer than the current ``stdout`` is
    not written (and not published), so an anomalous daemon cannot shorten
    stdout and scramble the per-subscriber cursors. Events are independent
    of text growth — a tool call may produce no visible text — and are
    always inserted (idempotently) before the shrink check decides whether
    the text write happens.
    """
    step = await db.get(StepModel, step_id)
    if step is None:
        raise ValueError({"type": "task_not_found", "message": step_id})
    if step.daemon_id != daemon_id:
        raise ValueError({"type": "task_not_owned", "message": step_id})

    if step.status == "cancelled":
        return {"recorded": False}

    normalized = normalize_agent_events(events)
    new_events = await _insert_agent_events(db, step_id, normalized)

    current = step.stdout or ""
    grew = len(stdout_prefix) > len(current)
    if grew:
        step.stdout = stdout_prefix
        step.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if not grew and not new_events:
        await db.commit()
        return {"recorded": False}

    await db.commit()

    if grew:
        default_hub.publish(
            step_id, {"type": "progress", "stdout_prefix": stdout_prefix}
        )
    if new_events:
        default_hub.publish(
            step_id, {"type": "agent_events", "events": new_events}
        )
    result: dict = {"recorded": True}
    if normalized:
        # Terminal-frame consumers only need this when events were sent;
        # keeping it out of legacy responses keeps them byte-identical.
        result["last_seq"] = await last_event_seq(db, step_id)
    return result


__all__ = [
    "record_step_result_for_daemon",
    "record_step_progress_for_daemon",
    "normalize_agent_events",
    "event_row_to_wire",
    "events_for_step",
    "last_event_seq",
    "step_usage_out",
    "usage_to_out",
]
