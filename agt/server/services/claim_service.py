"""Shared claim logic used by both HTTP and WS paths.

Both ``POST /api/tasks/claim`` (HTTP) and the WS ``tasks.claim`` RPC
delegate here so the SELECT/UPDATE/transaction logic lives in exactly
one place. Two paths calling the same DB code is the natural place for
bugs to diverge — one service function = one behavior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SessionModel, TaskModel
from ..schemas import TaskPayload
from .identity_service import _render_agent_md, get_identity

log = logging.getLogger(__name__)

# Module-level event used to wake up long-polling HTTP claim requests as
# soon as a new task is submitted. Best-effort: if the event fires
# spuriously, claimers simply re-query the DB and go back to sleep.
_task_available_event: asyncio.Event | None = None


def get_task_available_event() -> asyncio.Event:
    """Return the module-level task-available event, creating it lazily."""
    global _task_available_event
    if _task_available_event is None:
        _task_available_event = asyncio.Event()
    return _task_available_event


def notify_task_available() -> None:
    """Wake any HTTP claim requests currently long-polling for tasks."""
    event = _task_available_event
    if event is not None:
        event.set()


async def claim_tasks_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    max_tasks: int,
    timeout_seconds: int,
    runtime_ids: list[str] | None = None,
) -> list[TaskPayload]:
    """Long-poll for up to ``max_tasks`` pending tasks for ``daemon_id``.

    Returns immediately when at least one matching task is available.
    Otherwise waits up to ``timeout_seconds``, polling every 500 ms,
    and returns an empty list when the deadline passes.

    Provider routing (Step 3.4): when ``runtime_ids`` is provided, only
    tasks whose ``provider`` appears in the list are eligible. An empty
    ``runtime_ids`` list means "no providers declared" and yields no
    tasks (a daemon must declare what it can run before claiming).

    Identity rendering (Option D): when the claimed task carries an
    ``identity_did``, we look up the IdentityModel and inline the
    rendered AGENTS.md body into the payload as ``identity_md``. The
    daemon writes this verbatim into ``work_dir/IDENTITY.md`` and never
    needs to call back to the Server — which is impossible in a
    reverse-dial setup where the daemon sits behind NAT. Rendering at
    claim time (instead of submit time / on a cached daemon file)
    guarantees that any identity update is picked up by the next claim
    within one poll interval (~500ms).
    """
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 0.5
    event = get_task_available_event()

    # Step 3.4: explicit "I have nothing" short-circuits the poll. The
    # daemon declared no capabilities, so we honor that immediately
    # instead of burning the deadline in an empty loop.
    if runtime_ids is not None and not runtime_ids:
        return []

    while True:
        stmt = _build_select(daemon_id=daemon_id, max_tasks=max_tasks,
                             runtime_ids=runtime_ids)
        candidate = (await db.execute(stmt)).scalars().first()

        if candidate is not None:
            payload = await _claim_candidate(
                db, candidate=candidate, daemon_id=daemon_id,
            )
            await db.commit()
            return [payload]

        if time.monotonic() >= deadline:
            return []

        # Sleep until the next poll, but wake immediately when a task is
        # submitted via ``notify_task_available``.
        try:
            await asyncio.wait_for(event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass
        else:
            # Woken by a task submission. Clear the event so future
            # claimers block again; concurrent claimers all race to claim,
            # which is harmless because ``_claim_candidate`` is atomic.
            if event.is_set():
                event.clear()
        await db.rollback()


def _build_select(
    *,
    daemon_id: str,
    max_tasks: int,
    runtime_ids: list[str] | None,
):
    """Build the long-poll SELECT statement for claimable tasks.

    Pulled out of ``claim_tasks_for_daemon`` so the daemon-targeting /
    provider-filter / LIMIT logic is easy to read and the outer loop
    stays focused on polling.
    """
    stmt = select(TaskModel).where(
        TaskModel.status == "pending",
        # Step 3.6: daemon targeting. Free tasks (target is NULL) are
        # claimable by anyone; targeted tasks are visible only to the
        # daemon they name. Both classes coexist in the same queue.
        or_(
            TaskModel.target_daemon_id.is_(None),
            TaskModel.target_daemon_id == daemon_id,
        ),
    )
    # Step 3.4: provider-aware filter. Empty ``runtime_ids`` is handled
    # in the caller (early return). Here we only add the ``in_`` clause
    # when the daemon actually declared capabilities.
    if runtime_ids is not None:
        stmt = stmt.where(TaskModel.provider.in_(runtime_ids))
    # Note: the SELECT/LIMIT is currently 1 — one task per long-poll
    # call (long-poll semantics). max_tasks is reserved for a future
    # batch-claim path and is normalized here to keep the signature
    # stable.
    effective_limit = max(1, int(max_tasks))
    stmt = stmt.order_by(TaskModel.created_at).limit(effective_limit)
    return stmt


async def _claim_candidate(
    db: AsyncSession,
    *,
    candidate: TaskModel,
    daemon_id: str,
) -> TaskPayload:
    """Mark ``candidate`` running, look up its session + identity, build
    the payload that goes back to the daemon.

    The caller is responsible for ``db.commit()`` — keeping that outside
    lets tests stage the work and inspect intermediate state if needed.
    """
    candidate.status = "running"
    candidate.daemon_id = daemon_id
    candidate.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    session_rec = (
        await db.execute(
            select(SessionModel).where(
                SessionModel.task_id == candidate.task_id,
                SessionModel.provider == candidate.provider,
            )
        )
    ).scalars().first()
    resume_session_id = session_rec.session_id if session_rec else None

    # Option D: render the identity's AGENTS.md here so the daemon can
    # write it directly without a callback to the Server. Defensive: a
    # missing identity (e.g. deleted after submit) shouldn't fail the
    # claim — the task just runs without an injected identity, which
    # matches the pre-Option-D silent-skip semantics.
    identity_md = await _render_identity_md(db, candidate)

    return TaskPayload(
        task_id=candidate.task_id,
        provider=candidate.provider,
        prompt=candidate.prompt,
        resume_session_id=resume_session_id,
        work_dir=None,
        identity_did=candidate.identity_did,
        identity_md=identity_md,
        agent_cwd=candidate.agent_cwd,
    )


async def _render_identity_md(
    db: AsyncSession, candidate: TaskModel,
) -> str | None:
    """Render the identity AGENTS.md body for the claimed task.

    Returns ``None`` when the task has no identity, or when the
    referenced identity has been deleted since submit (claim is best-
    effort — never fails on a missing identity).
    """
    if candidate.identity_did is None:
        return None
    ident = await get_identity(candidate.identity_did, db)
    if ident is None:
        log.warning(
            "task %s references missing identity %s; "
            "claiming without identity injection",
            candidate.task_id, candidate.identity_did,
        )
        return None
    return _render_agent_md(ident)


__all__ = ["claim_tasks_for_daemon"]
