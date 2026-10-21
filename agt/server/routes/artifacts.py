"""Artifact routes (Phase 2): public metadata + content resolution."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import ArtifactModel, StepModel
from ..schemas import ArtifactOut, ArtifactUploadRequest
from ..services.artifact_service import (
    ArtifactFetchError,
    artifact_to_out,
    get_artifact,
    materialize_content,
    resolve_artifact_content,
)

router = APIRouter(prefix="/api/artifacts", tags=["artifacts"])


def _artifacts_dir(request: Request) -> Path:
    """Server blob directory, derived from the DB location."""
    cfg = request.app.state.config
    return Path(cfg.db_path).expanduser().parent / "artifacts"


def _fetch_hook_for(request: Request, artifact: ArtifactModel):
    """Build a lazy-fetch callable bound to the origin daemon's link."""

    async def _hook(model: ArtifactModel):
        links = getattr(request.app.state, "daemon_links", {})
        link = links.get(model.origin_daemon_id)
        if link is None or not link.connected or not link.supports_rpc_v1:
            return ArtifactFetchError(
                "producer_offline",
                "artifact not materialized (producer offline); retry later",
            )
        try:
            content = await link.fetch_artifact(
                model.artifact_id, model.local_path, model.sha256
            )
            return content
        except Exception as exc:
            reason = getattr(exc, "reason", None)
            if reason in ("producer_offline", "artifact_missing"):
                return ArtifactFetchError(reason, str(exc))
            return ArtifactFetchError("producer_offline", f"fetch failed: {exc}")

    return _hook


def _content_response(content: bytes, artifact: ArtifactModel) -> Response:
    """Serve bytes with untrusted-mime hardening (design D8)."""
    mime = artifact.mime_type or "application/octet-stream"
    safe_name = artifact.name.replace("\\", "/").split("/")[-1] or "artifact"
    return Response(
        content=content,
        media_type=mime,
        headers={
            "Content-Disposition": f'attachment; filename="{safe_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/{artifact_id}", response_model=ArtifactOut)
async def artifact_metadata(
    artifact_id: str,
    db: AsyncSession = Depends(get_db),
) -> ArtifactOut:
    artifact = await get_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
        )
    return ArtifactOut(**artifact_to_out(artifact))


@router.get("/{artifact_id}/content")
async def artifact_content(
    artifact_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Resolve and serve an artifact's bytes.

    Inline/blob content serves directly; producer-local-only content
    triggers the lazy fetch (server→origin-daemon RPC). Producer offline
    surfaces an explicit retryable 409; a gone producer file marks the
    row ``lost`` and surfaces 410.
    """
    artifact = await get_artifact(db, artifact_id)
    if artifact is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="artifact not found"
        )
    try:
        content, _ = await resolve_artifact_content(
            db,
            artifact,
            artifacts_dir=_artifacts_dir(request),
            fetch_hook=_fetch_hook_for(request, artifact),
        )
    except ArtifactFetchError as exc:
        if exc.reason == "artifact_missing":
            code = status.HTTP_410_GONE
        else:
            code = status.HTTP_409_CONFLICT
        raise HTTPException(status_code=code, detail=exc.message) from None
    if content is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="artifact not materialized; retry later",
        )
    return _content_response(content, artifact)


@router.post("", response_model=ArtifactOut, status_code=status.HTTP_201_CREATED)
async def upload_artifact(
    payload: ArtifactUploadRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ArtifactOut:
    """Eager upload from an HTTP-reachable daemon (pre-report).

    Ownership mirrors the report path: only the daemon that claimed the
    Step may upload for it. Content is sha256-verified then materialized
    (inline <4KB / server disk) — the artifact lands ``ready`` in one
    step, so the subsequent report can reference it metadata-only.
    """
    step = await db.get(StepModel, payload.task_id)
    if step is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found"
        )
    if step.daemon_id != payload.daemon_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="task not owned by this daemon",
        )
    if step.job_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="task has no job; cannot register artifacts",
        )

    from datetime import datetime, timezone
    from uuid import uuid4

    try:
        content = base64.b64decode(payload.content_b64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="invalid base64 content"
        ) from None

    item = payload.artifact
    # Idempotency: an eager upload followed by the report's metadata
    # entry must not duplicate the row — match on (step, name, sha256).
    from sqlalchemy import select

    prior = (
        await db.execute(
            select(ArtifactModel).where(
                ArtifactModel.step_id == step.step_id,
                ArtifactModel.name == item.name,
                ArtifactModel.sha256 == item.sha256,
            )
        )
    ).scalars().first()
    if prior is not None:
        await db.rollback()
        return ArtifactOut(**artifact_to_out(prior))

    artifact = ArtifactModel(
        artifact_id=str(uuid4()),
        job_id=step.job_id,
        step_id=step.step_id,
        producer_identity_id=step.identity_did,
        name=item.name,
        mime_type=item.mime_type or "",
        summary=item.summary,
        status_flag=item.status_flag,
        size=item.size,
        sha256=item.sha256,
        local_path=item.local_path,
        origin_daemon_id=payload.daemon_id,
        status="registered",
        created_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
    )
    db.add(artifact)
    await db.flush()
    try:
        materialize_content(artifact, content, _artifacts_dir(request))
    except ValueError as exc:
        await db.rollback()
        detail = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=detail.get("message", "content hash mismatch"),
        ) from None
    await db.commit()
    await db.refresh(artifact)
    return ArtifactOut(**artifact_to_out(artifact))
