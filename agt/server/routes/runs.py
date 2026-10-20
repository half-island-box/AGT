"""Unified run-history API across Chat, SOP, and Goal modes."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import RunListResponse, RunSummary
from ..services.chat_view import list_chat_runs
from ..services.goal_view import list_goal_runs
from ..services.sop_view import list_sop_runs

router = APIRouter(prefix="/api/runs", tags=["runs"])


@router.get("", response_model=RunListResponse)
async def list_runs(
    cwd: str | None = Query(None, description="Project directory to filter by"),
    mode: Literal["chat", "sop", "goal"] | None = Query(None, description="Mode filter"),
    daemon_id: str | None = Query(None, description="Optional daemon filter"),
    provider: str | None = Query(None, description="Optional provider filter"),
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> RunListResponse:
    """List runs across chat/sop/goal modes, optionally filtered by cwd.

    When ``mode`` is omitted, results from all three modes are merged and
    paginated together. The ``cwd`` parameter is the execution directory
    (client cwd for local runs, daemon-side ``--dir`` for remote runs).
    """
    modes: list[Literal["chat", "sop", "goal"]] = (
        [mode] if mode is not None else ["chat", "sop", "goal"]
    )

    chunks: list[tuple[list[RunSummary], int]] = []
    for m in modes:
        if m == "goal":
            runs, total = await list_goal_runs(
                db, cwd=cwd, daemon_id=daemon_id, limit=limit, offset=offset
            )
            chunks.append(
                (
                    [
                        RunSummary(
                            root_job_id=r.root_job_id,
                            mode="goal",
                            status=r.status,
                            provider=r.provider,
                            identity_did=r.identity_did,
                            daemon_id=r.daemon_id,
                            cwd=r.cwd,
                            title=r.goal,
                            created_at=r.created_at,
                            updated_at=r.updated_at,
                        )
                        for r in runs
                    ],
                    total,
                )
            )
        elif m == "sop":
            runs, total = await list_sop_runs(
                db, cwd=cwd, daemon_id=daemon_id, limit=limit, offset=offset
            )
            chunks.append(
                (
                    [
                        RunSummary(
                            root_job_id=r.root_job_id,
                            mode="sop",
                            status=r.status,
                            provider=r.provider,
                            identity_did=r.identity_did,
                            daemon_id=r.daemon_id,
                            cwd=r.cwd,
                            title=r.name,
                            created_at=r.created_at,
                            updated_at=r.updated_at,
                        )
                        for r in runs
                    ],
                    total,
                )
            )
        else:  # chat
            runs, total = await list_chat_runs(
                db,
                cwd=cwd,
                daemon_id=daemon_id,
                provider=provider,
                limit=limit,
                offset=offset,
            )
            chunks.append(
                (
                    [
                        RunSummary(
                            root_job_id=r.root_job_id,
                            mode="chat",
                            status=r.status,
                            provider=r.provider,
                            identity_did=r.identity_did,
                            daemon_id=r.daemon_id,
                            cwd=r.cwd,
                            title=r.title,
                            created_at=r.created_at,
                            updated_at=r.updated_at,
                        )
                        for r in runs
                    ],
                    total,
                )
            )

    if len(chunks) == 1:
        runs, total = chunks[0]
        return RunListResponse(runs=runs, total=total)

    # Merge across modes: sort by updated_at desc then apply limit/offset.
    total = sum(t for _, t in chunks)
    all_runs: list[RunSummary] = []
    for runs, _ in chunks:
        all_runs.extend(runs)
    all_runs.sort(key=lambda r: r.updated_at, reverse=True)
    page = all_runs[offset : offset + limit]
    return RunListResponse(runs=page, total=total)
