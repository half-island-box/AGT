"""Task submission, claiming, reporting, status routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import RuntimeModel, SessionModel, TaskModel
from ..schemas import (
    ClaimRequest,
    ClaimResponse,
    ReportRequest,
    ReportResponse,
    SubmitTaskRequest,
    SubmitTaskResponse,
    TaskStatusResponse,
)
from ..services.claim_service import claim_tasks_for_daemon

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _new_task_id() -> str:
    return str(uuid.uuid4())[:8]


@router.post("/submit", response_model=SubmitTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_task(
    payload: SubmitTaskRequest,
    db: AsyncSession = Depends(get_db),
) -> SubmitTaskResponse:
    """Insert a task in pending state.

    If ``payload.task_id`` matches a terminal task (completed/failed/
    timeout), it is reopened as a fresh pending turn so the same task_id
    can drive session continuity across turns. An *active* (pending/
    running) task_id is rejected with 409.
    """
    # Step 3.6: when a target daemon is named, require that daemon to have
    # registered the requested provider — otherwise the task would sit
    # pending forever (the target can never claim it). Free tasks skip this.
    # Validated up front so it applies to both new and reopened tasks.
    if payload.target_daemon_id:
        provider_registered = (
            await db.execute(
                select(RuntimeModel).where(
                    RuntimeModel.daemon_id == payload.target_daemon_id,
                    RuntimeModel.provider == payload.provider,
                )
            )
        ).scalars().first()
        if provider_registered is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"daemon '{payload.target_daemon_id}' has no runtime "
                    f"for provider '{payload.provider}'"
                ),
            )

    task_id = payload.task_id or _new_task_id()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    existing = await db.get(TaskModel, task_id)
    if existing is not None:
        if existing.status in ("pending", "running"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task_id '{task_id}' is already active",
            )
        # Terminal task → reopen for a resume turn. Reset execution fields
        # but keep the session pointer (SessionModel) so the next claim
        # resumes the same agent session.
        existing.provider = payload.provider
        existing.prompt = payload.prompt
        existing.status = "pending"
        existing.target_daemon_id = payload.target_daemon_id
        existing.daemon_id = None
        existing.exit_code = None
        existing.stdout = None
        existing.stderr = None
        existing.duration = None
        existing.updated_at = now
        await db.commit()
        return SubmitTaskResponse(task_id=task_id, status="pending")

    db.add(
        TaskModel(
            task_id=task_id,
            provider=payload.provider,
            prompt=payload.prompt,
            status="pending",
            target_daemon_id=payload.target_daemon_id,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return SubmitTaskResponse(task_id=task_id, status="pending")


@router.post("/claim", response_model=ClaimResponse)
async def claim_task(
    payload: ClaimRequest,
    db: AsyncSession = Depends(get_db),
) -> ClaimResponse:
    """HTTP entry point for ``claim_tasks_for_daemon``.

    The wire format (``{daemon_id, max_concurrent, timeout_seconds,
    runtime_ids}`` → ``{tasks: [...]}``) is preserved for backward
    compatibility. The reverse-dial ``DaemonLink`` (server side) uses
    the same service function over WebSocket.
    """
    tasks = await claim_tasks_for_daemon(
        db,
        daemon_id=payload.daemon_id,
        max_tasks=payload.max_concurrent,
        timeout_seconds=payload.timeout_seconds,
        runtime_ids=payload.runtime_ids,
    )
    return ClaimResponse(tasks=tasks)


@router.post("/{task_id}/report")
async def report_task(
    task_id: str,
    payload: ReportRequest,
    db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    """Daemon reports the result of an executed task."""
    task = await db.get(TaskModel, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    if task.daemon_id != payload.daemon_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="task not owned by this daemon"
        )

    task.exit_code = payload.exit_code
    task.stdout = payload.stdout
    task.stderr = payload.stderr
    task.duration = payload.duration
    task.status = payload.status
    task.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    if payload.resume_rejected:
        await db.execute(
            SessionModel.__table__.delete().where(
                (SessionModel.task_id == task_id) & (SessionModel.provider == task.provider)
            )
        )
        task.session_id = None
    elif payload.session_id:
        existing = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.task_id == task_id, SessionModel.provider == task.provider
                )
            )
        ).scalars().first()
        if existing is None:
            db.add(
                SessionModel(
                    task_id=task_id,
                    provider=task.provider,
                    session_id=payload.session_id,
                    created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
                )
            )
        else:
            existing.session_id = payload.session_id
        task.session_id = payload.session_id

    await db.commit()
    return ReportResponse(recorded=True)


@router.get("/{task_id}/status")
async def task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskStatusResponse:
    task = await db.get(TaskModel, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return TaskStatusResponse(
        task_id=task.task_id,
        provider=task.provider,
        status=task.status,
        session_id=task.session_id,
        exit_code=task.exit_code,
        stdout=task.stdout,
        stderr=task.stderr,
        duration=task.duration,
        created_at=task.created_at,
        updated_at=task.updated_at,
        daemon_id=task.daemon_id,
        target_daemon_id=task.target_daemon_id,
    )


__all__ = ["router"]
