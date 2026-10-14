"""Loop advancement service for SOP cyclic workflows.

When every job of the current loop iteration reaches a terminal state,
this service either:

- materialises the next iteration (if ``current < max_iterations``), or
- transitions the loop sentinel to ``done`` so external DAG dependents wake.

It is invoked from :func:`job_service.try_wake_dependents` after any loop
job becomes terminal.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobModel, JobParentModel
from .job_service import (
    _TERMINAL_STATUSES,
    _now,
    create_job,
    transition_job,
    try_wake_dependents,
)
from .sop_service import activate_job

log = logging.getLogger(__name__)


async def check_loop_advance(db: AsyncSession, root_job_id: str) -> None:
    """Check every loop under ``root_job_id`` and advance/finish it."""
    loops = await _loop_states(db, root_job_id)
    if not loops:
        return
    for loop_name, state in loops.items():
        await _advance_one_loop(db, root_job_id, loop_name, state)


async def _loop_states(db: AsyncSession, root_job_id: str) -> dict[str, dict]:
    """Group loop jobs by loop name, returning sentinel + jobs per iteration."""
    rows = (
        await db.execute(
            select(JobModel).where(
                JobModel.root_job_id == root_job_id,
                JobModel.sop_loop_name.is_not(None),
            )
        )
    ).scalars().all()
    states: dict[str, dict] = {}
    for job in rows:
        name = job.sop_loop_name
        if name not in states:
            meta = json.loads(job.sop_loop_meta) if job.sop_loop_meta else {}
            states[name] = {
                "max_iterations": meta.get("max_iterations", 1),
                "entry": meta.get("entry"),
                "sentinel": None,
                "jobs": {},
            }
        if job.sop_iteration == 0:
            states[name]["sentinel"] = job.job_id
        else:
            states[name]["jobs"].setdefault(job.sop_iteration, []).append(job)
    return states


async def _advance_one_loop(
    db: AsyncSession, root_job_id: str, loop_name: str, state: dict
) -> None:
    sentinel_id = state["sentinel"]
    if not sentinel_id:
        return

    sentinel = await db.get(JobModel, sentinel_id)
    if sentinel is None or sentinel.status in _TERMINAL_STATUSES:
        return

    if not state["jobs"]:
        return

    current_iter = max(state["jobs"].keys())
    current_jobs = state["jobs"][current_iter]

    # Fail fast: any failed/cancelled job kills the whole loop.
    if any(j.status in {"failed", "cancelled"} for j in current_jobs):
        await _fail_sentinel(db, sentinel)
        return

    if not all(j.status in _TERMINAL_STATUSES for j in current_jobs):
        return

    if current_iter < state["max_iterations"]:
        await _materialize_iteration(
            db, root_job_id, loop_name, current_iter + 1, current_jobs, state
        )
    else:
        await _complete_sentinel(db, sentinel)


async def _fail_sentinel(db: AsyncSession, sentinel: JobModel) -> None:
    if sentinel.status in _TERMINAL_STATUSES:
        return
    await transition_job(db, sentinel.job_id, "dependencies_satisfied")
    await transition_job(db, sentinel.job_id, "step_failed")
    await try_wake_dependents(db, sentinel.job_id)


async def _complete_sentinel(db: AsyncSession, sentinel: JobModel) -> None:
    if sentinel.status in _TERMINAL_STATUSES:
        return
    # Sentinel is created blocked (no Step). Walk it through the state
    # machine to done so external dependents wake cleanly.
    await transition_job(db, sentinel.job_id, "dependencies_satisfied")
    await transition_job(db, sentinel.job_id, "step_claimed")
    await transition_job(db, sentinel.job_id, "step_done")
    await try_wake_dependents(db, sentinel.job_id)


async def _materialize_iteration(
    db: AsyncSession,
    root_job_id: str,
    loop_name: str,
    iteration: int,
    prev_jobs: list[JobModel],
    state: dict,
) -> None:
    """Create iteration ``iteration`` jobs for every node in the loop."""
    template_jobs = {j.job_id: j for j in state["jobs"].get(1, [])}
    if not template_jobs:
        return

    template_ids = list(template_jobs.keys())
    parent_rows = (
        await db.execute(
            select(JobParentModel.job_id, JobParentModel.parent_job_id).where(
                JobParentModel.job_id.in_(template_ids)
            )
        )
    ).all()

    internal_edges: dict[str, list[str]] = {tid: [] for tid in template_ids}
    external_parents: dict[str, list[str]] = {tid: [] for tid in template_ids}

    parent_cache: dict[str, JobModel | None] = {}
    for row in parent_rows:
        parent_id = row.parent_job_id
        if parent_id not in parent_cache:
            parent_cache[parent_id] = await db.get(JobModel, parent_id)
        parent = parent_cache[parent_id]
        if parent is None:
            continue
        if parent.sop_loop_name == loop_name and parent.sop_iteration == 1:
            internal_edges[row.job_id].append(parent_id)
        else:
            external_parents[row.job_id].append(parent_id)

    template_id_to_name = {
        jid: json.loads(j.sop_node).get("name", jid)
        for jid, j in template_jobs.items()
        if j.sop_node
    }
    name_to_template_id = {name: jid for jid, name in template_id_to_name.items()}

    new_ids: dict[str, str] = {}
    entry_template_id = name_to_template_id.get(state["entry"])

    # Pass 1: create every job with external parents only (so all target ids
    # exist before we wire internal edges in pass 2).
    for tid, template in template_jobs.items():
        meta = json.loads(template.sop_node) if template.sop_node else {}
        meta["iteration"] = iteration

        parents = list(external_parents[tid])
        if tid == entry_template_id:
            parents.extend(j.job_id for j in prev_jobs)

        job = await create_job(
            db,
            provider=template.provider or meta.get("provider_hint") or "claude",
            prompt="",
            identity_did=template.identity_did,
            with_step=False,
            parents=parents,
            root_job_id=root_job_id,
            sop_node=json.dumps(meta, ensure_ascii=False),
            sop_loop_name=loop_name,
            sop_iteration=iteration,
            sop_loop_meta=template.sop_loop_meta,
        )
        new_ids[tid] = job.job_id

    # Pass 2: add internal loop edges (template A -> template B becomes
    # new A -> new B). These are dependency edges within the same iteration.
    now = _now()
    for tid, parent_tids in internal_edges.items():
        for parent_tid in parent_tids:
            db.add(
                JobParentModel(
                    job_id=new_ids[tid],
                    parent_job_id=new_ids[parent_tid],
                    created_at=now,
                )
            )

    await db.flush()

    # Activate the entry node: all its parents are terminal by construction.
    if entry_template_id:
        entry_job = await db.get(JobModel, new_ids[entry_template_id])
        if entry_job is not None:
            await activate_job(db, entry_job)
