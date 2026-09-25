"""Goal-mode routes: one requirement → one root orchestrator Job."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import GoalRunListResponse
from ..services.goal_export import export_goal
from ..services.goal_service import (
    DEFAULT_MAX_BATCH,
    DEFAULT_MAX_DEPTH,
    DEFAULT_MAX_DESCENDANTS,
    submit_goal,
)
from ..services.goal_view import list_goal_runs
from ..services.selection_service import SelectionError
from .identities import _selection_error_to_http
from .tasks import _notify_daemons

router = APIRouter(prefix="/api/goal", tags=["goal"])


class GoalSubmitRequest(BaseModel):
    goal: str
    identity_did: str | None = None
    provider: str | None = None
    daemon_id: str | None = None
    dir: str | None = None
    max_depth: int = Field(default=DEFAULT_MAX_DEPTH, ge=1, le=10)
    max_descendants: int = Field(default=DEFAULT_MAX_DESCENDANTS, ge=1, le=512)


class GoalSubmitResponse(BaseModel):
    root_job_id: str
    status: str


@router.get("/runs", response_model=GoalRunListResponse)
async def goal_runs(
    db: AsyncSession = Depends(get_db),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> GoalRunListResponse:
    """List goal roots, newest first, with a per-run tree status summary."""
    runs, total = await list_goal_runs(db, limit=limit, offset=offset)
    return GoalRunListResponse(runs=runs, total=total)


@router.post("/submit", response_model=GoalSubmitResponse, status_code=status.HTTP_201_CREATED)
async def goal_submit(
    payload: GoalSubmitRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> GoalSubmitResponse:
    """Create the goal root Job (validation fails fast; see goal_service)."""
    try:
        job = await submit_goal(
            db,
            goal=payload.goal,
            identity_did=payload.identity_did,
            provider=payload.provider,
            daemon_id=payload.daemon_id,
            dir=payload.dir,
            max_depth=payload.max_depth,
            max_descendants=payload.max_descendants,
        )
    except SelectionError as exc:
        raise _selection_error_to_http(exc)
    except ValueError as exc:
        detail = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail.get("message", str(exc)),
        ) from None
    await db.commit()
    await _notify_daemons(request, job.target_daemon_id, job.provider)
    return GoalSubmitResponse(root_job_id=job.job_id, status=job.status)


class GoalExportResponse(BaseModel):
    yaml: str
    warnings: list[str] = []


@router.get("/{root_job_id}/export", response_model=GoalExportResponse)
async def goal_export(
    root_job_id: str,
    include_failed: bool = False,
    keep_planning: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Export a goal run's Step-event graph as a SOP YAML draft."""
    try:
        result = await export_goal(
            db,
            root_job_id,
            include_failed=include_failed,
            keep_planning=keep_planning,
        )
    except ValueError as exc:
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        etype = payload.get("type", "")
        if etype == "job_not_found":
            code = status.HTTP_404_NOT_FOUND
        elif etype == "not_goal_root":
            code = status.HTTP_400_BAD_REQUEST
        else:
            code = status.HTTP_500_INTERNAL_SERVER_ERROR
        raise HTTPException(
            status_code=code,
            detail=payload.get("message", str(exc)),
        ) from None
    return GoalExportResponse(yaml=result["yaml"], warnings=result.get("warnings", []))


__all__ = ["router"]
