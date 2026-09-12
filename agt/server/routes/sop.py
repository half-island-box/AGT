"""SOP routes (Phase 5): DAG submission and status."""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import make_artifact_uri
from ...sop.dsl import SOPError, parse_sop
from ..database import get_db
from ..models import ArtifactModel, JobModel, JobParentModel, TaskModel
from ..services.claim_service import notify_task_available
from ..services.sop_service import expand_sop

router = APIRouter(prefix="/api/sop", tags=["sop"])


class SopSubmitRequest(BaseModel):
    """Parsed SOP document (CLI validates locally, server re-validates).

    ``cwd`` / ``local_daemon_id`` carry the submitter's pwd and local
    daemon id so ``expand_sop`` can default an un-pinned node's
    ``agent_cwd`` to pwd when it lands on the local daemon (and refuse a
    remote landing with no ``dir``).
    """

    spec: dict
    cwd: str | None = None
    local_daemon_id: str | None = None


class SopSubmitResponse(BaseModel):
    root_job_id: str
    nodes: dict[str, str]


class SopNodeStatus(BaseModel):
    name: str
    job_id: str
    status: str
    depends_on: list[str] = []
    artifacts: list[str] = []
    provider: str | None = None
    session_id: str | None = None


class SopStatusResponse(BaseModel):
    root_job_id: str
    nodes: list[SopNodeStatus]
    all_terminal: bool


class SopNodeLogStep(BaseModel):
    """One Step's full execution record for a SOP node."""

    task_id: str
    status: str
    session_id: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None


class SopNodeLogResponse(BaseModel):
    node_name: str
    job_id: str
    status: str
    provider: str | None = None
    steps: list[SopNodeLogStep] = []


@router.post("/submit", response_model=SopSubmitResponse)
async def submit_sop(
    payload: SopSubmitRequest,
    db: AsyncSession = Depends(get_db),
) -> SopSubmitResponse:
    """Validate + expand a SOP DAG into shared-root Jobs (one transaction)."""
    try:
        spec = parse_sop(payload.spec)
        expansion = await expand_sop(
            db, spec, cwd=payload.cwd, local_daemon_id=payload.local_daemon_id
        )
    except SOPError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    except ValueError as exc:
        body = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=body.get("message", str(exc)),
        ) from None
    await db.commit()
    # Wake claim loops: entry nodes are immediately runnable.
    notify_task_available()
    return SopSubmitResponse(
        root_job_id=expansion.root_job_id, nodes=expansion.node_jobs
    )


@router.get("/{root_job_id}/status", response_model=SopStatusResponse)
async def sop_status(
    root_job_id: str,
    db: AsyncSession = Depends(get_db),
) -> SopStatusResponse:
    """Per-node DAG status. Only DAG nodes are listed (review G8): jobs
    under this root that carry ``sop_node`` metadata — dynamic-spawn
    descendants (no metadata) are excluded."""
    rows = (
        await db.execute(
            select(JobModel).where(
                JobModel.root_job_id == root_job_id,
                JobModel.sop_node.is_not(None),
            )
        )
    ).scalars().all()
    if not rows:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="sop not found"
        )

    by_job_id = {j.job_id: j for j in rows}
    terminal = {"done", "failed", "cancelled"}

    # Batch-fetch the most recent session_id per job (one per node).
    job_ids = [j.job_id for j in rows]
    step_rows = (
        await db.execute(
            select(TaskModel.job_id, TaskModel.session_id)
            .where(TaskModel.job_id.in_(job_ids))
            .where(TaskModel.session_id.is_not(None))
            .order_by(TaskModel.created_at.desc())
        )
    ).all()
    job_to_session: dict[str, str] = {}
    for job_id, session_id in step_rows:
        if job_id not in job_to_session:
            job_to_session[job_id] = session_id

    nodes: list[SopNodeStatus] = []
    for job in rows:
        meta = json.loads(job.sop_node) if job.sop_node else {}
        dep_names = []
        for link in (
            await db.execute(
                select(JobParentModel.parent_job_id).where(
                    JobParentModel.job_id == job.job_id
                )
            )
        ).scalars().all():
            up = by_job_id.get(link)
            if up is not None and up.sop_node:
                dep_names.append(json.loads(up.sop_node).get("name", link))
        arts = (
            await db.execute(
                select(ArtifactModel).where(ArtifactModel.job_id == job.job_id)
            )
        ).scalars().all()

        # Provider: activated nodes write it to job.provider; blocked nodes
        # still carry the hint in sop_node metadata.
        provider = (
            job.provider
            or meta.get("explicit_provider")
            or meta.get("provider_hint")
        )

        nodes.append(
            SopNodeStatus(
                name=meta.get("name", job.job_id),
                job_id=job.job_id,
                status=job.status,
                depends_on=dep_names,
                artifacts=[make_artifact_uri(a.artifact_id) for a in arts],
                provider=provider,
                session_id=job_to_session.get(job.job_id),
            )
        )
    return SopStatusResponse(
        root_job_id=root_job_id,
        nodes=nodes,
        all_terminal=all(n.status in terminal for n in nodes),
    )


@router.get("/{root_job_id}/nodes/{node_name}/log", response_model=SopNodeLogResponse)
async def sop_node_log(
    root_job_id: str,
    node_name: str,
    db: AsyncSession = Depends(get_db),
) -> SopNodeLogResponse:
    """Full execution log (stdout/stderr) for one SOP node, by name.

    Resolves the node name against each DAG node's ``sop_node.name``
    (JSON column), then returns every Step under that Job in creation
    order — not just the latest — so the whole execution history of the
    node is observable. 404 when the node does not exist under this root.
    """
    rows = (
        await db.execute(
            select(JobModel).where(
                JobModel.root_job_id == root_job_id,
                JobModel.sop_node.is_not(None),
            )
        )
    ).scalars().all()

    job: JobModel | None = None
    for candidate in rows:
        meta = json.loads(candidate.sop_node) if candidate.sop_node else {}
        if meta.get("name") == node_name:
            job = candidate
            break

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"node '{node_name}' not found under sop '{root_job_id}'",
        )

    steps = (
        await db.execute(
            select(TaskModel)
            .where(TaskModel.job_id == job.job_id)
            .order_by(TaskModel.created_at)
        )
    ).scalars().all()

    meta = json.loads(job.sop_node) if job.sop_node else {}
    provider = (
        job.provider
        or meta.get("explicit_provider")
        or meta.get("provider_hint")
    )

    return SopNodeLogResponse(
        node_name=node_name,
        job_id=job.job_id,
        status=job.status,
        provider=provider,
        steps=[
            SopNodeLogStep(
                task_id=s.task_id,
                status=s.status,
                session_id=s.session_id,
                exit_code=s.exit_code,
                stdout=s.stdout,
                stderr=s.stderr,
            )
            for s in steps
        ],
    )


__all__ = ["router"]
