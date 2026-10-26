"""Job lifecycle routes."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import ArtifactModel, JobModel, StepModel
from ..schemas import ArtifactOut, GoalMetaSummary, SubmitTaskRequest, SubmitTaskResponse
from ..services.artifact_service import artifact_to_out, artifacts_for_steps
from ..services.goal_view import ask_question, current_task_id, goal_meta_summary
from ..services.claim_service import notify_steps_available
from ..services.job_service import (
    append_step,
    cancel_job,
    create_job,
    get_job,
    stop_chat_job,
)
from ..services.selection_service import SelectionError, select_runtime
from .identities import _selection_error_to_http
from .tasks import _notify_daemons
from ...a2a import Message, render_message

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


class CreateJobRequest(SubmitTaskRequest):
    """Reuses SubmitTaskRequest shape minus the client-supplied task_id."""

    task_id: None = None  # type: ignore[assignment]


class CreateJobResponse(SubmitTaskResponse):
    job_id: str


class JobInputRequest(BaseModel):
    prompt: str
    # Phase 2: optional structured input for this turn.
    message: Message | None = None


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    current_step_id: str | None = None
    # Phase 2: declared deliverables of the current Step (public form).
    artifacts: list[ArtifactOut] = []


class JobTreeNode(BaseModel):
    """One node of a Job subtree (goal / spawn lineage view)."""

    job_id: str
    parent_job_id: str | None = None
    status: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    session_id: str | None = None
    # identity slug first, else the goal / first-prompt excerpt (≤24 chars).
    label: str = ""
    current_task_id: str | None = None
    ask_question: str | None = None
    # Wall seconds between Job creation and last update (both are
    # "%Y-%m-%d %H:%M:%S" UTC strings).
    duration: float | None = None
    artifacts: list[ArtifactOut] = []
    # First line of the latest failed/timeout Step's stderr, truncated.
    error: str | None = None
    children: list["JobTreeNode"] = []


class JobTreeResponse(BaseModel):
    root_job_id: str
    node: JobTreeNode
    goal: GoalMetaSummary | None = None


class JobLogStep(BaseModel):
    """One Step's full execution record."""

    task_id: str
    status: str
    session_id: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


class JobLogResponse(BaseModel):
    job_id: str
    status: str
    steps: list[JobLogStep] = []


@router.post("", response_model=CreateJobResponse, status_code=status.HTTP_201_CREATED)
async def create_job_route(
    payload: CreateJobRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CreateJobResponse:
    """Create a Job with its first pending Step."""
    effective_provider = payload.provider
    effective_target = payload.target_daemon_id
    prompt = (
        render_message(payload.message) if payload.message else payload.prompt
    )

    if payload.identity_did:
        try:
            result = await select_runtime(
                payload.identity_did,
                payload.provider,
                payload.target_daemon_id,
                db,
            )
        except SelectionError as exc:
            raise _selection_error_to_http(exc)
        effective_provider = result.provider
        effective_target = result.daemon_id

    job = await create_job(
        db,
        provider=effective_provider,
        prompt=prompt,
        target_daemon_id=effective_target,
        work_dir=payload.work_dir,
        agent_cwd=payload.agent_cwd,
        identity_did=payload.identity_did,
        job_type="chat",
        message=payload.message,
    )
    await db.commit()
    await _notify_daemons(request, effective_target, effective_provider)
    return CreateJobResponse(
        job_id=job.job_id,
        task_id=job.steps[0].step_id,
        status=job.status,
    )


@router.post("/{job_id}/input")
async def job_input(
    job_id: str,
    payload: JobInputRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> CreateJobResponse:
    """Append a new Step to a Job (e.g. next chat turn)."""
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    if job.status != "waiting_input":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"job is {job.status}, expected waiting_input",
        )

    prompt = (
        render_message(payload.message) if payload.message else payload.prompt
    )
    step = await append_step(db, job_id, prompt, message=payload.message)
    await db.commit()
    await _notify_daemons(request, job.target_daemon_id, job.provider)
    return CreateJobResponse(
        job_id=job_id,
        task_id=step.step_id,
        status=job.status,
    )


@router.post("/{job_id}/stop")
async def job_stop(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Stop the current chat turn and keep the Job available for input."""
    try:
        job, notify_list = await stop_chat_job(db, job_id)
    except ValueError as exc:
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        error_type = payload.get("type", "")
        if error_type == "job_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="job not found",
            )
        if error_type in {
            "stop_not_supported",
            "stop_has_children",
            "no_running_step",
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=payload.get("message", str(payload)),
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(payload or exc),
        )
    await db.commit()

    # Same dual-channel cancellation used by /cancel: reverse-dial daemons
    # receive an immediate RPC; HTTP long-poll daemons receive the cancelled
    # task id with their next claim response.
    daemon_links: dict = getattr(request.app.state, "daemon_links", {})
    for daemon_id, task_id in notify_list:
        link = daemon_links.get(daemon_id)
        if link is not None:
            try:
                await link.notify_cancel(task_id)
            except Exception:
                pass
    notify_steps_available()
    return {
        "job_id": job.job_id,
        "status": job.status,
        "interrupted_task_ids": [task_id for _, task_id in notify_list],
    }


@router.post("/{job_id}/cancel")
async def job_cancel(
    job_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Cancel a Job, cascade the subtree, and propagate cancellation.

    Phase 4 dual-channel delivery: reverse-dial daemons get a
    best-effort ``tasks.cancel`` RPC; HTTP long-poll daemons get the
    cancellation delivered via their next claim response (the claim
    event is woken so that happens promptly, not after the 30s poll).
    """
    try:
        _, notify_list = await cancel_job(db, job_id)
    except ValueError as exc:
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        if payload.get("type") == "job_not_found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
            ) from None
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(payload)) from None
    await db.commit()

    # Best-effort propagation (never blocks the cancel itself):
    # 1) reverse-dial daemons — tasks.cancel over the WS link;
    # 2) local long-poll daemons — wake the claim event so the next
    #    claim response carries cancelled_task_ids immediately.
    daemon_links: dict = getattr(request.app.state, "daemon_links", {})
    for notify_daemon, task_id in notify_list:
        link = daemon_links.get(notify_daemon)
        if link is None:
            continue
        try:
            await link.notify_cancel(task_id)
        except Exception:
            pass  # log-and-continue: the claim channel still delivers
    notify_steps_available()
    return {"cancelled": True}
    return {"cancelled": True}


@router.get("/{job_id}/status")
async def job_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")

    current = None
    for step in reversed(job.steps):
        if step.status in ("running", "pending", "completed", "failed", "timeout", "cancelled"):
            current = step.step_id
            break

    artifact_rows = (
        await artifacts_for_steps(db, [current] if current else [])
    ).get(current or "", [])

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        current_step_id=current,
        artifacts=[
            ArtifactOut(**artifact_to_out(r)) for r in artifact_rows
        ],
    )


# ── GET /{job_id}/tree — Job subtree view (goal mode observability) ─


def _ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _node_label(job: JobModel) -> str:
    """identity slug first, else the goal / first prompt excerpt."""
    if job.identity_did and ":" in job.identity_did:
        return job.identity_did.rsplit(":", 1)[-1]
    first_prompt = job.steps[0].prompt if job.steps else ""
    text = " ".join((first_prompt or "").split())
    return text[:24]


def _node_error(job: JobModel) -> str | None:
    for step in reversed(job.steps):
        if step.status in ("failed", "timeout") and step.stderr:
            first_line = step.stderr.strip().splitlines()[0]
            return first_line[:160]
    return None


def _build_tree_node(
    job: JobModel,
    artifacts_by_job: dict[str, list[ArtifactModel]],
    children_of: dict[str, list[JobModel]],
) -> JobTreeNode:
    daemon_id = next(
        (s.daemon_id for s in reversed(job.steps) if s.daemon_id), None
    )
    session_id = next(
        (s.session_id for s in reversed(job.steps) if s.session_id), None
    )
    created, updated = _ts(job.created_at), _ts(job.updated_at)
    duration = (
        max(0.0, (updated - created).total_seconds())
        if created and updated else None
    )
    return JobTreeNode(
        job_id=job.job_id,
        parent_job_id=job.parent_job_id,
        status=job.status,
        provider=job.provider,
        identity_did=job.identity_did,
        daemon_id=daemon_id,
        session_id=session_id,
        label=_node_label(job),
        current_task_id=current_task_id(job),
        ask_question=ask_question(job),
        duration=duration,
        artifacts=[
            ArtifactOut(**artifact_to_out(r))
            for r in artifacts_by_job.get(job.job_id, [])
        ],
        error=_node_error(job),
        children=[
            _build_tree_node(child, artifacts_by_job, children_of)
            for child in children_of.get(job.job_id, [])
        ],
    )


@router.get("/{job_id}/tree")
async def job_tree(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> JobTreeResponse:
    """Nested Job subtree rooted at ``job_id`` (spawn lineage).

    One query by ``root_job_id`` — a root's own ``root_job_id`` equals
    its ``job_id``, so the single condition covers the root itself.
    Nodes are assembled by ``parent_job_id`` and ordered by creation
    time. Backs ``agt goal status`` / ``goal run`` (goal mode) and
    works for any spawn-rooted Job.
    """
    rows = (
        await db.execute(
            select(JobModel)
            .where(JobModel.root_job_id == job_id)
            .order_by(JobModel.created_at, JobModel.job_id)
        )
    ).scalars().all()

    by_id = {j.job_id: j for j in rows}
    root = by_id.get(job_id)
    if root is None:
        # Either unknown id (404) or a non-root node: for the latter,
        # return that node alone as a single-node subtree view.
        root = await db.get(JobModel, job_id)
        if root is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="job not found"
            )
        rows = [root]
        by_id = {root.job_id: root}

    children_of: dict[str, list[JobModel]] = {}
    for job in rows:
        if job.parent_job_id and job.parent_job_id in by_id:
            children_of.setdefault(job.parent_job_id, []).append(job)

    artifact_rows = (
        await db.execute(
            select(ArtifactModel).where(
                ArtifactModel.job_id.in_([j.job_id for j in rows])
            )
        )
    ).scalars().all()
    artifacts_by_job: dict[str, list[ArtifactModel]] = {}
    for row in artifact_rows:
        artifacts_by_job.setdefault(row.job_id, []).append(row)

    return JobTreeResponse(
        root_job_id=root.job_id,
        node=_build_tree_node(root, artifacts_by_job, children_of),
        goal=goal_meta_summary(root),
    )


@router.get("/{job_id}/log", response_model=JobLogResponse)
async def job_log(
    job_id: str,
    db: AsyncSession = Depends(get_db),
) -> JobLogResponse:
    """Full stdout/stderr for every Step under a Job, in creation order.

    Generic (works for goal nodes and any spawn Job); SOP keeps its own
    name-addressed view. Backs ``agt goal log <root> [job_id]``.
    """
    job = await get_job(db, job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobLogResponse(
        job_id=job.job_id,
        status=job.status,
        steps=[
            JobLogStep(
                task_id=s.step_id,
                status=s.status,
                session_id=s.session_id,
                exit_code=s.exit_code,
                stdout=s.stdout,
                stderr=s.stderr,
            )
            for s in job.steps
        ],
    )


__all__ = ["router"]
