"""Job lifecycle service.

A Job is the long-running owner of a multi-step task. Each Step is an
immutable ``StepModel`` row. The Job state machine is the single entry
point for status transitions so the rest of the codebase can't leave a
Job in an inconsistent state.
"""

from __future__ import annotations

import asyncio
import uuid
import json
import logging
import weakref
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ...a2a import (
    DataPart,
    FilePart,
    Message,
    TextPart,
    make_artifact_uri,
    message_from_prompt,
    render_message,
)
from ..models import (
    ArtifactModel,
    JobModel,
    JobParentModel,
    SessionModel,
    StepModel,
)

log = logging.getLogger(__name__)

_VALID_STATUSES = {
    "queued", "running", "waiting_input", "waiting_children", "blocked",
    "done", "failed", "cancelled",
}
_TERMINAL_STATUSES = {"done", "failed", "cancelled"}

# Phase 3 spawn budgets (spec: subjob-orchestration / spawn 预算).
MAX_SPAWN_BATCH = 8
MAX_TREE_DEPTH = 3
MAX_ROOT_CHILDREN = 32


_child_done_locks: weakref.WeakValueDictionary[
    tuple[int, str], asyncio.Lock
] = weakref.WeakValueDictionary()


def _child_done_lock(parent_job_id: str) -> asyncio.Lock:
    """Serialize synthesis creation for one parent within this event loop."""
    loop = asyncio.get_running_loop()
    key = (id(loop), parent_job_id)
    lock = _child_done_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _child_done_locks[key] = lock
    return lock


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _elapsed_seconds(started_at: str | None) -> float:
    """Best-effort elapsed time for an interrupted Step."""
    if not started_at:
        return 0.0
    try:
        started = datetime.strptime(started_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0.0
    return max(0.0, (datetime.now(timezone.utc) - started).total_seconds())


def _mark_step_interrupted(step: StepModel) -> None:
    """Make a Server-side interruption terminal on the Step itself.

    A late daemon report may still arrive, but report handling treats a
    pre-cancelled Step as historical cleanup and never lets it drive the
    parent Job again. ``exit_code`` remains NULL until a cancellation
    channel has actually delivered the id to its owning daemon.
    """
    step.status = "cancelled"
    step.duration = step.duration if step.duration is not None else _elapsed_seconds(
        step.created_at
    )
    step.outcome = step.outcome or "error"
    step.updated_at = _now()


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
    cwd: str | None = None,
    identity_did: str | None = None,
    job_id: str | None = None,
    task_id: str | None = None,
    job_type: str = "task",
    message: Message | None = None,
    parent_job_id: str | None = None,
    with_step: bool = True,
    parents: list[str] | None = None,
    root_job_id: str | None = None,
    sop_node: str | None = None,
    goal_meta: str | None = None,
    sop_loop_name: str | None = None,
    sop_iteration: int = 0,
    sop_loop_meta: str | None = None,
) -> JobModel:
    """Create a Job and (by default) its first pending Step.

    ``message`` is the structured (A2A-shaped) input; when omitted the
    plain ``prompt`` is normalized into a single-TextPart Message so every
    new Step row carries a canonical ``input_message``.

    Phase 5 (SOP) additions:

    - ``with_step=False`` — create an EMPTY Job (no Step) with
      ``status="blocked"``: dependency-gated SOP nodes stay out of the
      claim pool until activated (``append_step`` at wake time).
    - ``parents`` — upstream job ids for the DAG edges; each becomes a
      ``job_parents`` row (this job waits on each parent). All ids must
      exist (ValueError otherwise).
    - ``root_job_id`` — explicit root override. SOP nodes share the root
      WITHOUT a spawn ``parent_job_id``, so the default "root = self"
      would silently break the shared-root invariant (review G2).
    - ``cwd`` — explicit run-history directory (submitter project cwd).
      It takes precedence over ``work_dir`` / ``agent_cwd``; ``agent_cwd``
      remains the daemon-side execution directory.
    """
    now = _now()
    j_id = job_id or _new_id()
    s_id = task_id or _new_id()

    # Phase 6: normalize the project directory used for run isolation.
    # Prefer work_dir (local client cwd); fall back to agent_cwd (daemon-side
    # execution directory). Resolve to an absolute path without trailing slash.
    raw_cwd = cwd or work_dir or agent_cwd
    try:
        effective_cwd = str(Path(raw_cwd).resolve()) if raw_cwd else None
    except (OSError, ValueError):
        effective_cwd = raw_cwd

    # Phase 3: lineage — children inherit the parent's root; roots carry
    # their own id (denormalized for subtree/budget queries). Phase 5:
    # an explicit override wins (SOP nodes share a root with no lineage).
    effective_root = root_job_id or j_id
    if parent_job_id is not None:
        parent = await db.get(JobModel, parent_job_id)
        if parent is None:
            raise ValueError({"type": "parent_job_not_found", "message": parent_job_id})
        effective_root = root_job_id or parent.root_job_id
        # Children run in the same project context as their parent by default.
        if effective_cwd is None:
            effective_cwd = parent.cwd

    blocked = not with_step
    job = JobModel(
        job_id=j_id,
        parent_job_id=parent_job_id,
        root_job_id=effective_root,
        status="blocked" if blocked else "queued",
        job_type=job_type,
        first_prompt=prompt if job_type == "chat" else None,
        provider=provider,
        identity_did=identity_did,
        target_daemon_id=target_daemon_id,
        work_dir=work_dir,
        agent_cwd=agent_cwd,
        cwd=effective_cwd,
        sop_node=sop_node,
        goal_meta=goal_meta,
        sop_loop_name=sop_loop_name,
        sop_iteration=sop_iteration,
        sop_loop_meta=sop_loop_meta,
        created_at=now,
        updated_at=now,
    )
    db.add(job)

    # Phase 5: DAG dependency edges — this job waits on each parent.
    for upstream in parents or []:
        if await db.get(JobModel, upstream) is None:
            raise ValueError({"type": "parent_job_not_found", "message": upstream})
        db.add(
            JobParentModel(
                job_id=j_id,
                parent_job_id=upstream,
                created_at=now,
            )
        )

    if with_step:
        step = StepModel(
            step_id=s_id,
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
        # Wire the Step into the Job's in-memory collection so callers can
        # read ``job.steps`` without an async lazy-load round-trip.
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
    target_daemon_id: str | None = None,
) -> StepModel:
    """Append a new pending Step to a Job.

    The Job MUST be in a state that accepts new Steps (queued, running, or
    waiting_input). The new Step inherits its runtime metadata from the Job.
    """
    # Eagerly load the steps collection: async sessions cannot lazy-load,
    # and callers (including SOP activation) read ``job.steps`` below.
    result = await db.execute(
        select(JobModel).where(JobModel.job_id == job_id).options(selectinload(JobModel.steps))
    )
    job = result.scalar_one_or_none()
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})
    if job.status not in {"queued", "running", "waiting_input"}:
        raise ValueError({
            "type": "job_not_accepting_input",
            "message": f"job {job_id} is {job.status}",
        })

    now = _now()
    step = StepModel(
        step_id=_new_id(),
        job_id=job_id,
        provider=job.provider,
        prompt=prompt,
        status="pending",
        # Phase 3: explicit target wins (synthesis pinning, D10) —
        # free parents would otherwise let synthesis land anywhere.
        target_daemon_id=target_daemon_id or job.target_daemon_id,
        session_id=resume_session_id,
        work_dir=job.work_dir,
        agent_cwd=job.agent_cwd,
        identity_did=job.identity_did,
        input_message=(message or message_from_prompt(prompt)).model_dump_json(),
        created_at=now,
        updated_at=now,
    )
    db.add(step)
    # Wire the Step into the Job's in-memory collection (mirrors
    # ``create_job``): with ``expire_on_commit=False`` the identity map
    # keeps a stale ``job.steps`` after commit, so callers that re-read
    # the Job in the same session would otherwise miss the new Step.
    job.steps.append(step)
    await db.flush()

    # Moving from waiting_input back to running means a new Step is ready.
    if job.status == "waiting_input":
        await transition_job(db, job_id, "input_received")

    return step


async def claim_step(db: AsyncSession, step_id: str) -> JobModel:
    """Mark a Step running and move the Job from queued -> running if needed."""
    step = await db.get(StepModel, step_id)
    if step is None or step.job_id is None:
        raise ValueError({"type": "step_not_found", "message": step_id})
    return await transition_job(db, step.job_id, "step_claimed")


async def report_step(
    db: AsyncSession,
    step_id: str,
    outcome: str | None,
) -> JobModel:
    """Process a terminal Step report and drive the Job state machine.

    ``outcome`` is one of: done, needs_input, error, spawn_subjob. When None,
    the caller is responsible for deriving it before calling this function.
    """
    step = await db.get(StepModel, step_id)
    if step is None or step.job_id is None:
        raise ValueError({"type": "step_not_found", "message": step_id})

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
        # Phase 5 (SOP): a node may fail at activation (e.g. missing
        # upstream output) before ever being claimed — allow the terminal
        # failure event straight from queued.
        "queued": {
            "step_claimed": "running",
            "step_failed": "failed",
            "step_interrupted": "waiting_input",
            "cancel": "cancelled",
        },
        "running": {
            "step_done": "done",
            "step_needs_input": "waiting_input",
            "step_failed": "failed",
            "step_spawned": "waiting_children",
            "step_interrupted": "waiting_input",
            "cancel": "cancelled",
        },
        "waiting_input": {"input_received": "running", "cancel": "cancelled"},
        "waiting_children": {"children_done": "running", "cancel": "cancelled"},
        # Phase 5 (SOP): created blocked, activates when all upstream
        # dependencies reach a terminal state.
        "blocked": {"dependencies_satisfied": "queued", "cancel": "cancelled"},
    }
    return transitions.get(current, {}).get(event, current)


async def cancel_job(db: AsyncSession, job_id: str) -> tuple[JobModel, list[tuple[str, str]]]:
    """Cancel a Job, cascading to its whole descendant subtree (D15).

    Descendants are found by ``parent_job_id`` closure — NOT by
    ``root_job_id`` (that would cancel sibling branches too). Both
    running AND pending Steps flip to ``cancelled`` (review fix #8: a
    pending step that stays pending would still be claimed, run to
    completion, and its report would bypass the cancelled guard —
    hollowing out the cancel).

    Returns ``(job, notify_list)`` where notify_list is
    ``[(daemon_id, task_id)]`` for running steps that were cancelled —
    the caller (route layer) sends best-effort ``tasks.cancel`` /
    claim-channel delivery and wakes the long-poll event. After the
    cascade, a cancelled node's parent gets its wakeup check.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})

    notify_list: list[tuple[str, str]] = []
    subtree_ids = await _descendant_closure(db, job_id)
    dependent_ids = await _dependent_closure(db, job_id)
    # Cascade = the job itself + spawn descendants (D15) + downstream DAG
    # dependents (Phase 5 D6). Both closures are exclusive of the root.
    cancel_set = [job_id] + subtree_ids + [d for d in dependent_ids if d != job_id]
    seen: set[str] = set()
    ordered = [jid for jid in cancel_set if not (jid in seen or seen.add(jid))]
    for jid in ordered:
        target = job if jid == job_id else await get_job(db, jid)
        if target is None:
            continue
        for step in target.steps:
            if step.status in ("running", "pending"):
                if step.status == "running" and step.daemon_id:
                    notify_list.append((step.daemon_id, step.step_id))
                _mark_step_interrupted(step)
        await transition_job(db, jid, "cancel")

    # Wakeup check for the cancelled node's parent (no-op if the parent
    # is itself terminal — e.g. the cascade came from higher up).
    if job.parent_job_id is not None:
        await on_child_done(db, job.parent_job_id)

    return job, notify_list


async def stop_chat_job(
    db: AsyncSession, job_id: str
) -> tuple[JobModel, list[tuple[str, str]]]:
    """Interrupt the current chat turn but keep the conversation resumable.

    This is intentionally different from :func:`cancel_job`: cancellation is
    terminal, while stop marks the interrupted Step terminal and moves the
    Chat Job back to ``waiting_input`` so the user can ask a follow-up turn.
    A turn is interruptable both before and after daemon claim: queueing is
    part of the user-visible turn, not a window in which stop must fail.

    Only a simple Chat Job is supported. Jobs that have spawned children use
    the terminal cancel path; allowing their subtree to continue while the
    parent waits for input would make the result tree ambiguous.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise ValueError({"type": "job_not_found", "message": job_id})
    if job.job_type != "chat":
        raise ValueError({
            "type": "stop_not_supported",
            "message": f"job {job_id} is not a chat job",
        })
    if job.status == "waiting_input":
        return job, []
    if job.status not in ("queued", "running"):
        raise ValueError({
            "type": "stop_not_supported",
            "message": f"job {job_id} is {job.status}, expected queued or running",
        })

    descendants = await _descendant_closure(db, job_id)
    if descendants:
        raise ValueError({
            "type": "stop_has_children",
            "message": (
                f"job {job_id} has spawned children; use cancel instead"
            ),
        })

    notify_list: list[tuple[str, str]] = []
    active_steps = [
        step for step in job.steps if step.status in ("pending", "running")
    ]
    if not active_steps:
        raise ValueError({
            "type": "no_running_step",
            "message": f"job {job_id} has no active step to stop",
        })
    for step in active_steps:
        # Include pending Steps in the response / SSE closure path even
        # though there is no owning daemon to notify yet.
        notify_list.append((step.daemon_id or "", step.step_id))
        _mark_step_interrupted(step)

    await transition_job(db, job_id, "step_interrupted")
    return job, notify_list


async def _descendant_closure(db: AsyncSession, job_id: str) -> list[str]:
    """All descendant job ids of ``job_id`` (exclusive), iteratively
    expanding ``parent_job_id`` (SQLite-friendly, no recursive CTE)."""
    seen: list[str] = []
    frontier = [job_id]
    while frontier:
        rows = (
            await db.execute(
                select(JobModel.job_id).where(JobModel.parent_job_id.in_(frontier))
            )
        ).scalars().all()
        frontier = [r for r in rows if r not in seen]
        seen.extend(frontier)
    return seen


async def _dependent_closure(db: AsyncSession, job_id: str) -> list[str]:
    """All downstream DAG dependents of ``job_id`` (exclusive), expanding
    ``job_parents.parent_job_id → job_id`` iteratively."""
    seen: list[str] = []
    frontier = [job_id]
    while frontier:
        rows = (
            await db.execute(
                select(JobParentModel.job_id).where(
                    JobParentModel.parent_job_id.in_(frontier)
                )
            )
        ).scalars().all()
        frontier = [r for r in rows if r not in seen]
        seen.extend(frontier)
    return seen


async def spawn_children(
    db: AsyncSession,
    *,
    parent_job: JobModel,
    children: list[dict],
    daemon_id: str,
    parent_step_id: str | None = None,
) -> list[JobModel]:
    """Compatibility wrapper for the legacy ``AGT-SPAWN`` report path."""
    if not children:
        return []
    from .spawn_service import spawn_children_detailed

    execution = await spawn_children_detailed(
        db,
        parent_job=parent_job,
        children=children,
        daemon_id=daemon_id,
        parent_step_id=parent_step_id,
        strict=False,
    )
    return execution.created_jobs


async def try_wake_dependents(db: AsyncSession, terminal_job_id: str) -> list[str]:
    """Wake dependency-gated dependents of a (just-)terminal Job (SOP).

    For each DIRECT dependent (``job_parents.parent_job_id == terminal``):
    skip unless still ``blocked``; if ALL its upstream jobs are terminal,
    hand it to ``sop_service.activate_job`` (deferred import — that module
    imports this one). Returns the activated job ids (empty for non-SOP
    jobs with no dependents, so callers may invoke liberally from any
    terminal path).

    The activation itself may end in ``failed`` (missing upstream
    output); sop_service re-invokes this function there so failure
    propagates down the chain (review G1).

    Phase 5 loops: a terminal loop job may also allow the next iteration
    (or loop completion) to proceed.
    """
    terminal_job = await db.get(JobModel, terminal_job_id)
    if terminal_job is not None and terminal_job.sop_loop_name:
        from .sop_loop_service import check_loop_advance

        await check_loop_advance(db, terminal_job.root_job_id)

    dep_rows = (
        await db.execute(
            select(JobParentModel.job_id).where(
                JobParentModel.parent_job_id == terminal_job_id
            )
        )
    ).scalars().all()
    if not dep_rows:
        return []

    activated: list[str] = []
    from .sop_service import activate_job  # deferred: avoid import cycle

    for dep_id in dep_rows:
        dep = await db.get(JobModel, dep_id)
        if dep is None or dep.status != "blocked":
            continue
        parent_ids = (
            await db.execute(
                select(JobParentModel.parent_job_id).where(
                    JobParentModel.job_id == dep_id
                )
            )
        ).scalars().all()
        parents = [await db.get(JobModel, pid) for pid in parent_ids]
        if any(p is None or p.status not in _TERMINAL_STATUSES for p in parents):
            continue
        await activate_job(db, dep)
        activated.append(dep_id)
    return activated


async def on_child_done(
    db: AsyncSession,
    parent_job_id: str,
    *,
    synthesis_daemon_id: str | None = None,
) -> None:
    """Wake a ``waiting_children`` parent once all children are terminal.

    No-op when the parent is missing/terminal/not waiting — callers may
    invoke it liberally from any child-terminal path (report hook,
    cancel, failed-at-birth spawn). On full completion the parent
    transitions ``children_done → running`` and a synthesis Step is
    appended, pinned to the children's executing daemon (D10).
    """
    async with _child_done_lock(parent_job_id):
        parent = await get_job(db, parent_job_id)
        if parent is None or parent.status != "waiting_children":
            return

        children = (
            await db.execute(
                select(JobModel).where(JobModel.parent_job_id == parent_job_id)
            )
        ).scalars().all()
        if not children:
            return
        if not all(c.status in _TERMINAL_STATUSES for c in children):
            return

        # Pin synthesis to the children's daemon: explicit hint, else the
        # daemon that ran the parent's spawn step.
        daemon_id = synthesis_daemon_id
        if daemon_id is None:
            for step in reversed(parent.steps):
                if step.daemon_id:
                    daemon_id = step.daemon_id
                    break

        await transition_job(db, parent_job_id, "children_done")
        message = await _synthesis_message(db, children)
        await append_step(
            db,
            parent_job_id,
            render_message(message),
            message=message,
            target_daemon_id=daemon_id,
        )


async def _synthesis_message(db: AsyncSession, children: list[JobModel]) -> Message:
    """Build the synthesis Step input (spec: synthesis 聚合).

    Per-child entries carry ``job_status`` (source: JobModel.status) —
    Job-level outcome — while each artifact keeps its own product-level
    ``status_flag``/``summary``. A failed child with zero artifacts gets
    a bare ``job_status=failed`` entry (no fabricated flags).
    """
    entries: list[dict] = []
    file_parts: list[FilePart] = []
    for child in children:
        artifacts = (
            await db.execute(
                select(ArtifactModel).where(ArtifactModel.job_id == child.job_id)
            )
        ).scalars().all()
        artifact_entries = [
            {
                "uri": make_artifact_uri(a.artifact_id),
                "name": a.name,
                "status_flag": a.status_flag,
                "summary": a.summary,
            }
            for a in artifacts
        ]
        entries.append({
            "job_id": child.job_id,
            "identity": child.identity_did,
            "job_status": child.status,
            "artifacts": artifact_entries,
        })
        for a in artifacts:
            file_parts.append(FilePart(uri=make_artifact_uri(a.artifact_id)))

    return Message(
        role="user",
        parts=[
            TextPart(
                text="你的子任务已全部完成。请综合下列交付物（含各子任务状态）给出最终结果。"
            ),
            DataPart(data={"children": entries}),
            *file_parts,
        ],
    )


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
    "try_wake_dependents",
    "on_child_done",
    "spawn_children",
    "resolve_job_by_session",
]
