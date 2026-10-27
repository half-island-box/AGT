"""HTTP surfaces for Goal shared context."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..middleware.auth import verify_task_token
from ..models import ContextEntryModel, JobModel, StepModel
from ..services.context_service import (
    context_read_for_caller,
    context_retract_for_caller,
    context_write_for_caller,
    resolve_context_caller,
)

router = APIRouter(tags=["context"])


class ContextWriteRequest(BaseModel):
    scope: str = Field(min_length=1)
    key: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    content: dict
    idempotency_key: str = Field(min_length=1, max_length=128)
    expected_head_entry_id: int | None = None


class ContextRetractRequest(BaseModel):
    key: str = Field(min_length=1)
    expected_head_entry_id: int
    idempotency_key: str = Field(min_length=1, max_length=128)


def _entry_out(row: ContextEntryModel) -> dict:
    import json

    return {
        "entry_id": row.entry_id,
        "root_job_id": row.root_job_id,
        "author_job_id": row.author_job_id,
        "author_step_id": row.author_step_id,
        "scope": row.scope,
        "key": row.key,
        "kind": row.kind,
        "content": json.loads(row.content_json),
        "status": row.status,
        "created_at": row.created_at,
    }


def _http_error(exc: ValueError) -> HTTPException:
    payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
    code = payload.get("code") or payload.get("type")
    if code in {"task_not_found", "job_not_found"}:
        return HTTPException(status_code=404, detail=payload.get("message", str(exc)))
    if code in {"task_not_owned", "context_permission_denied"}:
        return HTTPException(status_code=403, detail=payload.get("message", str(exc)))
    return HTTPException(status_code=400, detail=payload.get("message", str(exc)))


@router.post("/api/agent/context/write")
async def agent_context_write(
    payload: ContextWriteRequest,
    claims: dict = Depends(verify_task_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        caller = await resolve_context_caller(db, claims=claims, require_running=True)
        return await context_write_for_caller(
            db,
            caller,
            scope=payload.scope,
            key=payload.key,
            kind=payload.kind,
            content=payload.content,
            idempotency_key=payload.idempotency_key,
            expected_head_entry_id=payload.expected_head_entry_id,
        )
    except ValueError as exc:
        raise _http_error(exc) from None


@router.post("/api/agent/context/retract")
async def agent_context_retract(
    payload: ContextRetractRequest,
    claims: dict = Depends(verify_task_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        caller = await resolve_context_caller(db, claims=claims, require_running=True)
        return await context_retract_for_caller(
            db,
            caller,
            key=payload.key,
            expected_head_entry_id=payload.expected_head_entry_id,
            idempotency_key=payload.idempotency_key,
        )
    except ValueError as exc:
        raise _http_error(exc) from None


@router.get("/api/agent/context")
async def agent_context_read(
    keys: list[str] | None = Query(None),
    scopes: list[str] | None = Query(None),
    kinds: list[str] | None = Query(None),
    entry_id_after: int | None = None,
    limit: int | None = Query(None, ge=1, le=50),
    claims: dict = Depends(verify_task_token),
    db: AsyncSession = Depends(get_db),
) -> dict:
    try:
        caller = await resolve_context_caller(db, claims=claims, require_running=True)
        return await context_read_for_caller(
            db,
            caller,
            keys=keys,
            scopes=scopes,
            kinds=kinds,
            entry_id_after=entry_id_after,
            limit=limit,
        )
    except ValueError as exc:
        raise _http_error(exc) from None


@router.get("/api/goal/{root_job_id}/context")
async def goal_context_list(
    root_job_id: str,
    scope: str | None = None,
    kind: str | None = None,
    status_filter: str = Query("active", alias="status"),
    db: AsyncSession = Depends(get_db),
) -> dict:
    stmt = select(ContextEntryModel).where(ContextEntryModel.root_job_id == root_job_id)
    if scope:
        stmt = stmt.where(ContextEntryModel.scope == scope)
    if kind:
        stmt = stmt.where(ContextEntryModel.kind == kind)
    if status_filter:
        stmt = stmt.where(ContextEntryModel.status == status_filter)
    rows = (await db.execute(stmt.order_by(ContextEntryModel.entry_id.desc()))).scalars().all()
    return {"entries": [_entry_out(row) for row in rows]}


@router.get("/api/goal/{root_job_id}/context/{entry_id}")
async def goal_context_show(root_job_id: str, entry_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    row = await db.get(ContextEntryModel, entry_id)
    if row is None or row.root_job_id != root_job_id:
        raise HTTPException(status_code=404, detail="context entry not found")
    return _entry_out(row)


@router.get("/api/jobs/{job_id}/context-brief")
async def job_context_brief(job_id: str, db: AsyncSession = Depends(get_db)) -> dict:
    job = await db.get(JobModel, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    steps = (
        await db.execute(
            select(StepModel)
            .where(StepModel.job_id == job_id)
            .order_by(StepModel.created_at.desc(), StepModel.step_id.desc())
        )
    ).scalars().all()
    step = next(
        (item for item in steps if item.context_brief_json is not None),
        None,
    )
    if step is None:
        raise HTTPException(status_code=404, detail="context brief not found")
    import json

    return {
        "job_id": job_id,
        "step_id": step.step_id,
        "snapshot_id": step.context_snapshot_id,
        "watermark": step.context_watermark,
        "brief": json.loads(step.context_brief_json),
    }

