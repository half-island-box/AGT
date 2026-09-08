"""Job lifecycle routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import JobModel, TaskModel
from ..schemas import ArtifactOut, SubmitTaskRequest, SubmitTaskResponse
from ..services.artifact_service import artifact_to_out, artifacts_for_steps
from ..services.claim_service import notify_task_available
from ..services.job_service import append_step, cancel_job, create_job, get_job
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
                "local",
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
        task_id=job.steps[0].task_id,
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
        task_id=step.task_id,
        status=job.status,
    )


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
    notify_task_available()
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
            current = step.task_id
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


__all__ = ["router"]
