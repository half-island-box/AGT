"""Task submission, claiming, reporting, status routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import SessionModel, TaskModel
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
    """Insert a task in pending state. Reuses payload.task_id if provided (for session resume)."""
    task_id = payload.task_id or _new_task_id()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    existing = await db.get(TaskModel, task_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"task_id '{task_id}' already exists",
        )

    db.add(
        TaskModel(
            task_id=task_id,
            provider=payload.provider,
            prompt=payload.prompt,
            status="pending",
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
    )


__all__ = ["router"]
