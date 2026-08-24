"""Shared claim logic used by both HTTP and WS paths.

Both ``POST /api/tasks/claim`` (HTTP) and the WS ``tasks.claim`` RPC
delegate here so the SELECT/UPDATE/transaction logic lives in exactly
one place. Two paths calling the same DB code is the natural place for
bugs to diverge — one service function = one behavior.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import SessionModel, TaskModel
from ..schemas import TaskPayload


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
    """
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 0.5

    while True:
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
        # Step 3.4: provider-aware filter. Empty runtime_ids means the
        # daemon declared no capabilities → match nothing (not "match
        # all"). This forces daemons to register before claiming.
        if runtime_ids is not None:
            if not runtime_ids:
                return []  # explicit "I have nothing"; no point polling
            stmt = stmt.where(TaskModel.provider.in_(runtime_ids))
        # Note: the SELECT/LIMIT is currently 1 — one task per long-poll
        # call (long-poll semantics). max_tasks is reserved for a future
        # batch-claim path and is normalized here to keep the signature
        # stable.
        effective_limit = max(1, int(max_tasks))
        stmt = stmt.order_by(TaskModel.created_at).limit(effective_limit)

        candidate = (await db.execute(stmt)).scalars().first()

        if candidate is not None:
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

            payload = TaskPayload(
                task_id=candidate.task_id,
                provider=candidate.provider,
                prompt=candidate.prompt,
                resume_session_id=resume_session_id,
                work_dir=None,
                identity_did=candidate.identity_did,
                agent_cwd=candidate.agent_cwd,
            )
            await db.commit()
            return [payload]

        if time.monotonic() >= deadline:
            return []

        await asyncio.sleep(poll_interval)
        await db.rollback()


__all__ = ["claim_tasks_for_daemon"]
