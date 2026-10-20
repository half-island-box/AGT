"""Read-side helpers for SOP-mode run list APIs."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobModel
from ..schemas import SopRunListResponse, SopRunSummary


async def list_sop_runs(
    db: AsyncSession,
    *,
    cwd: str | None = None,
    daemon_id: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[SopRunSummary], int]:
    """List persisted SOP roots, newest update first, with tree summaries."""
    filters = [
        JobModel.root_job_id == JobModel.job_id,
        JobModel.job_type == "sop",
    ]
    if cwd is not None:
        filters.append(JobModel.cwd == cwd)
    if daemon_id is not None:
        filters.append(JobModel.target_daemon_id == daemon_id)

    roots = (
        (
            await db.execute(
                select(JobModel)
                .where(*filters)
                .order_by(JobModel.updated_at.desc(), JobModel.job_id.desc())
            )
        )
        .scalars()
        .all()
    )
    total = len(roots)
    page_roots = roots[offset : offset + limit]

    root_ids = [root.job_id for root in page_roots]
    nodes = []
    if root_ids:
        nodes = (
            (
                await db.execute(
                    select(JobModel)
                    .where(JobModel.root_job_id.in_(root_ids))
                    .order_by(JobModel.created_at, JobModel.job_id)
                )
            )
            .scalars()
            .all()
        )
    nodes_by_root: dict[str, list[JobModel]] = {}
    for node in nodes:
        nodes_by_root.setdefault(node.root_job_id, []).append(node)

    runs: list[SopRunSummary] = []
    for root in page_roots:
        meta = json.loads(root.sop_node) if root.sop_node else {}
        root_nodes = nodes_by_root.get(root.job_id, [])
        status_counts: dict[str, int] = {}
        for node in root_nodes:
            status_counts[node.status] = status_counts.get(node.status, 0) + 1
        runs.append(
            SopRunSummary(
                root_job_id=root.job_id,
                name=meta.get("name", root.job_id),
                status=root.status,
                provider=root.provider,
                identity_did=root.identity_did,
                daemon_id=root.target_daemon_id,
                cwd=root.cwd,
                created_at=root.created_at,
                updated_at=root.updated_at,
                total_nodes=len(root_nodes),
                status_counts=status_counts,
            )
        )
    return runs, total


async def list_sop_runs_response(
    db: AsyncSession,
    *,
    cwd: str | None = None,
    daemon_id: str | None = None,
    limit: int,
    offset: int,
) -> SopRunListResponse:
    runs, total = await list_sop_runs(
        db, cwd=cwd, daemon_id=daemon_id, limit=limit, offset=offset
    )
    return SopRunListResponse(runs=runs, total=total)
