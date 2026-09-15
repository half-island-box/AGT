"""Goal-mode submission (goal mode G2).

Thin entry over the existing Job machinery: validate → resolve runtime
→ inject the orchestrator template (D2) → ``create_job`` with
``goal_meta``. No new state machine — execution, spawning, synthesis
and cancellation all ride the claim/report hooks as-is.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...goal.prompt import with_orchestrator_template
from ..models import JobModel, RuntimeModel
from .job_service import create_job
from .runtime_service import is_stale
from .selection_service import select_runtime

# Goal-mode budgets (D3 + review: descendants, not direct children).
DEFAULT_MAX_DEPTH = 5
DEFAULT_MAX_DESCENDANTS = 64
DEFAULT_MAX_BATCH = 8
DEFAULT_PROVIDER = "claude"


async def _has_live_runtime(
    db: AsyncSession, *, provider: str, daemon_id: str | None
) -> bool:
    stmt = select(RuntimeModel).where(RuntimeModel.provider == provider)
    if daemon_id:
        stmt = stmt.where(RuntimeModel.daemon_id == daemon_id)
    rows = (await db.execute(stmt)).scalars().all()
    return any(not is_stale(r.last_seen) for r in rows)


async def submit_goal(
    db: AsyncSession,
    *,
    goal: str,
    identity_did: str | None = None,
    provider: str | None = None,
    daemon_id: str | None = None,
    dir: str | None = None,
    max_depth: int = DEFAULT_MAX_DEPTH,
    max_descendants: int = DEFAULT_MAX_DESCENDANTS,
) -> JobModel:
    """Create the goal root Job; raises ValueError/SelectionError on bad input.

    Prechecks (fail at submit, never silent-pending):
    - ``identity_did`` → ``select_runtime`` (exists + active + bound,
      resolves provider/target); the resolved target gets a liveness
      check like ``POST /api/tasks/submit``.
    - explicit ``(provider, daemon)`` pin → live runtime required.
    - no identity AND no daemon pin → at least one live runtime must
      offer the provider (review fix: otherwise the root pends forever).
    """
    if not goal or not goal.strip():
        raise ValueError({"type": "empty_goal", "message": "goal must be a non-empty string"})

    effective_provider = provider or DEFAULT_PROVIDER
    effective_target = daemon_id or None
    if identity_did:
        result = await select_runtime(identity_did, provider, "local", db)
        effective_provider = result.provider
        effective_target = result.daemon_id

    if effective_target:
        # Named daemon must offer the provider, else the root can never
        # be claimed (same rule as task submit).
        if not await _has_live_runtime(
            db, provider=effective_provider, daemon_id=effective_target
        ):
            raise ValueError({
                "type": "no_live_runtime",
                "message": (
                    f"daemon '{effective_target}' has no live runtime "
                    f"for provider '{effective_provider}'"
                ),
            })
    else:
        # Free task: SOME daemon must offer the provider (goal-mode
        # precheck; plain task submit keeps its looser legacy behavior).
        if not await _has_live_runtime(db, provider=effective_provider, daemon_id=None):
            raise ValueError({
                "type": "no_live_runtime",
                "message": (
                    f"no live runtime provides provider '{effective_provider}'"
                ),
            })

    # D2 injection: built-in template only when the root has NO identity.
    prompt = goal if identity_did else with_orchestrator_template(goal.strip())
    meta = {
        "mode": "goal",
        "budget": {
            "max_depth": max_depth,
            "max_descendants": max_descendants,
            "max_batch": DEFAULT_MAX_BATCH,
        },
        "template": "identity" if identity_did else "builtin",
        "goal": goal,
        "goal_sha256": hashlib.sha256(goal.encode("utf-8")).hexdigest(),
    }
    return await create_job(
        db,
        provider=effective_provider,
        prompt=prompt,
        target_daemon_id=effective_target,
        agent_cwd=dir,
        identity_did=identity_did,
        job_type="task",
        goal_meta=json.dumps(meta, ensure_ascii=False),
    )


__all__ = [
    "submit_goal",
    "DEFAULT_MAX_DEPTH",
    "DEFAULT_MAX_DESCENDANTS",
    "DEFAULT_MAX_BATCH",
]
