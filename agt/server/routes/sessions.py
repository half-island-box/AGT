"""Session lookup route.

Two query shapes (Phase 4 Step 3):

- ``GET /api/sessions?task_id=X&provider=Z[&identity_did=Y]`` — exact
  lookup of one session pointer, or 404. ``identity_did`` omitted means
  the identity-less (legacy) slot, ``""``.
- ``GET /api/sessions?task_id=X`` — list every session pointer for the
  task (the cross-agent view), so callers can surface "your prior
  session on claude is preserved but not carried over to codex".
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...agent_history import delete_provider_session, load_history
from ...comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain
from ...net import host_of_url, is_local_host
from ..database import get_db
from ..models import JobModel, SessionModel, TaskModel
from ...task.session import CwdSessionStore
from ..schemas import SessionHistoryOut, SessionResolveOut
from ..services.artifact_service import artifact_to_out, artifacts_for_job
from ..services.job_service import cancel_job

router = APIRouter(prefix="/api/sessions", tags=["sessions"])

_ACTIVE_SESSION_STATUSES = {
    "queued", "running", "waiting_input", "waiting_children", "blocked",
}


class SessionDeleteRequest(BaseModel):
    confirm: bool = False
    provider: str | None = None
    daemon_id: str | None = None
    finalize_active: bool = False


@router.get("")
async def get_session(
    job_id: str = Query(..., min_length=1),
    provider: str | None = Query(None),
    identity_did: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Exact session lookup (provider given) or job-wide list (provider omitted)."""
    ctx = identity_did or ""

    if provider is not None:
        rec = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.job_id == job_id,
                    SessionModel.identity_did == ctx,
                    SessionModel.provider == provider,
                )
            )
        ).scalars().first()
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no session for job"
            )
        return {
            "job_id": job_id,
            "identity_did": rec.identity_did,
            "provider": provider,
            "session_id": rec.session_id,
        }

    recs = (
        await db.execute(
            select(SessionModel).where(SessionModel.job_id == job_id)
        )
    ).scalars().all()
    return {
        "sessions": [
            {
                "job_id": r.job_id,
                "identity_did": r.identity_did,
                "provider": r.provider,
                "session_id": r.session_id,
            }
            for r in recs
        ]
    }


@router.get("/{session_id}")
async def resolve_session(
    session_id: str,
    db: AsyncSession = Depends(get_db),
) -> SessionResolveOut:
    """Reverse-lookup a session by id → job / provider / identity / dir.

    Backs remote ``agt chat --resume <sid> --daemon=X``. ``session_id`` is
    not the primary key (the SessionModel is keyed by job_id), so this
    reads the ``SessionModel`` row whose ``session_id`` matches, newest
    first as a defense against a reused id, then resolves the owning Job.
    """
    sess = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.session_id == session_id)
            .order_by(SessionModel.updated_at.desc())
        )
    ).scalars().first()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no session for id",
        )

    job = await db.get(JobModel, sess.job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session job not found",
        )

    # Return the most recent Step task_id as the canonical task_id for
    # backward-compatible callers; the CLI resume path uses job_id.
    latest_step = (
        await db.execute(
            select(TaskModel)
            .where(TaskModel.job_id == job.job_id)
            .order_by(TaskModel.updated_at.desc())
        )
    ).scalars().first()

    return SessionResolveOut(
        job_id=job.job_id,
        task_id=latest_step.task_id if latest_step else "",
        session_id=sess.session_id,
        provider=job.provider,
        identity_did=job.identity_did,
        daemon_id=latest_step.daemon_id if latest_step else None,
        dir=job.agent_cwd or job.work_dir,
    )


@router.get("/{session_id}/artifacts")
async def list_session_artifacts(
    session_id: str,
    daemon_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List a session's registered artifacts (Phase 2/3 deliverables).

    Backs ``agt artifact list --session <sid> --daemon <id>``. When
    ``daemon_id`` is given, the session MUST belong to that daemon —
    a mismatch is a hard 403 (never a silent cross-daemon leak). The
    owning daemon is the Job's pinned target, else the daemon that ran
    its latest Step.
    """
    sess = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.session_id == session_id)
            .order_by(SessionModel.updated_at.desc())
        )
    ).scalars().first()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="no session for id",
        )

    job = await db.get(JobModel, sess.job_id)
    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session job not found",
        )

    latest_step = (
        await db.execute(
            select(TaskModel)
            .where(TaskModel.job_id == job.job_id)
            .order_by(TaskModel.updated_at.desc())
        )
    ).scalars().first()
    owning_daemon = job.target_daemon_id or (
        latest_step.daemon_id if latest_step else None
    )

    if daemon_id is not None and owning_daemon != daemon_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"session {session_id} does not belong to daemon {daemon_id}",
        )

    return [artifact_to_out(r) for r in await artifacts_for_job(db, job.job_id)]


def _is_local_daemon_target(request: Request, daemon_id: str) -> bool:
    """Return True when the owning daemon target is on this machine.

    Reverse-dial remains the general path for remote daemons. For a local
    daemon, however, the Server process can read the same provider store
    (``~/.claude`` / ``~/.codex``) without requiring a WebSocket link.
    """
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        return False
    return any(
        target.daemon_id == daemon_id and is_local_host(host_of_url(target.url))
        for target in cfg.daemons
    )


async def _read_local_session_history(session_id: str, provider: str) -> dict:
    """Read a transcript directly from this machine's provider store."""
    try:
        messages = await asyncio.to_thread(load_history, provider, session_id)
    except NotImplementedError:
        return {"messages": [], "not_implemented": True}
    return {
        "messages": [
            {"role": m.role, "content": m.content, "timestamp": m.timestamp}
            for m in messages
        ],
        "not_implemented": False,
    }


async def _delete_local_session(
    provider: str,
    session_id: str,
    cwd: str | None,
) -> dict:
    """Run provider/cwd deletion in the Server process for a local daemon.

    This same-machine fallback exists because HTTP long-poll local daemons
    do not have a reverse-dial link. It uses the provider files and cwd
    store owned by the same user/home as the daemon.
    """
    result = await asyncio.to_thread(delete_provider_session, provider, session_id)
    if not result.supported:
        return {
            "ok": False,
            "not_implemented": True,
            "reason": result.reason,
            "deleted_paths": [],
            "cwd_index_deleted": False,
        }
    if result.errors:
        raise RuntimeError("; ".join(result.errors))

    cwd_index_deleted = False
    if cwd:
        cwd_index_deleted = await asyncio.to_thread(
            _delete_local_cwd_index, cwd, session_id, provider
        )
    return {
        "ok": True,
        "not_implemented": False,
        "deleted_paths": [str(path) for path in result.deleted_paths],
        "missing": result.missing,
        "cwd_index_deleted": cwd_index_deleted,
    }


def _delete_local_cwd_index(cwd: str, session_id: str, provider: str) -> bool:
    return CwdSessionStore.load_or_create(
        Path(cwd), provider=provider
    ).delete(session_id)


@router.post("/{session_id}/delete")
async def delete_session(
    session_id: str,
    request: Request,
    payload: SessionDeleteRequest,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete AGT session pointers and the owning provider transcript.

    Job/Task execution records are intentionally retained for audit; only
    their session pointers are cleared. A non-terminal Job is rejected so a
    live conversation cannot be deleted underneath its next turn.
    """
    if not payload.confirm:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="confirm must be true to delete a session",
        )

    sessions = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.session_id == session_id)
            .order_by(SessionModel.updated_at.desc())
        )
    ).scalars().all()
    if payload.provider:
        sessions = [row for row in sessions if row.provider == payload.provider]
    if not sessions:
        if not payload.provider or not payload.daemon_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="no session for id",
            )
        if _is_local_daemon_target(request, payload.daemon_id):
            provider_result = await _delete_local_session(
                payload.provider, session_id, None
            )
        else:
            daemon_links = getattr(request.app.state, "daemon_links", {})
            link = daemon_links.get(payload.daemon_id)
            try:
                provider_result = await link.delete_session(
                    session_id, payload.provider, None
                )
            except (WSRPCUnavailable, WSRPCUncertain):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="daemon offline during orphan transcript deletion",
                )
            except RPCRejected as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        "daemon rejected orphan transcript deletion: "
                        f"{exc.body}"
                    ),
                )
        if provider_result.get("not_implemented"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    provider_result.get("reason")
                    or "provider transcript deletion is not implemented: "
                    f"{payload.provider}"
                ),
            )
        if not provider_result.get("ok"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="daemon could not delete orphan provider transcript",
            )
        return {
            "ok": True,
            "session_id": session_id,
            "orphan_provider_transcript": True,
            "session_rows_deleted": 0,
            "task_pointers_cleared": 0,
            "provider_results": [provider_result],
        }
    if payload.daemon_id:
        matching_sessions: list[SessionModel] = []
        for sess in sessions:
            job = await db.get(JobModel, sess.job_id)
            latest = (
                await db.execute(
                    select(TaskModel)
                    .where(TaskModel.job_id == sess.job_id)
                    .order_by(TaskModel.updated_at.desc())
                )
            ).scalars().first()
            owning = latest.daemon_id if latest else None
            if owning is None and job is not None:
                owning = job.target_daemon_id
            if owning == payload.daemon_id:
                matching_sessions.append(sess)
        if not matching_sessions:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="session does not belong to daemon",
            )
        sessions = matching_sessions

    job_ids = {row.job_id for row in sessions}
    jobs = {
        job.job_id: job
        for job in (
            await db.execute(select(JobModel).where(JobModel.job_id.in_(job_ids)))
        ).scalars().all()
    }
    active_jobs = [
        job for job in jobs.values() if job.status in _ACTIVE_SESSION_STATUSES
    ]
    if active_jobs:
        waiting_jobs = [
            job for job in active_jobs if job.status == "waiting_input"
        ]
        non_waiting_jobs = [
            job for job in active_jobs if job.status != "waiting_input"
        ]
        if non_waiting_jobs or not payload.finalize_active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "session is still active; cancel it before deleting "
                    f"(job_status={active_jobs[0].status})"
                ),
            )
        if any(job.parent_job_id for job in waiting_jobs):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "waiting_input session has a parent; use cancel instead "
                    "of finalize-active deletion"
                ),
            )
        for job in waiting_jobs:
            await cancel_job(db, job.job_id)
        await db.flush()

    if payload.finalize_active:
        active_jobs = [
            job for job in jobs.values() if job.status in _ACTIVE_SESSION_STATUSES
        ]
        if active_jobs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "failed to finalize active session "
                    f"(job_status={active_jobs[0].status})"
                ),
            )

    tasks = (
        await db.execute(
            select(TaskModel).where(TaskModel.session_id == session_id)
        )
    ).scalars().all()
    session_by_job: dict[str, SessionModel] = {
        row.job_id: row for row in sessions
    }
    delete_targets: set[tuple[str, str | None, str | None]] = set()
    for task in tasks:
        job = jobs.get(task.job_id)
        provider = task.provider or (
            session_by_job.get(task.job_id).provider if task.job_id in session_by_job else None
        )
        daemon_id = task.daemon_id or (job.target_daemon_id if job else None)
        cwd = task.agent_cwd or (job.agent_cwd or job.work_dir if job else None)
        if provider:
            delete_targets.add((provider, daemon_id, cwd))
    for sess in sessions:
        if sess.job_id in session_by_job:
            job = jobs.get(sess.job_id)
            daemon_id = None
            cwd = job.agent_cwd or job.work_dir if job else None
            latest = next(
                (task for task in tasks if task.job_id == sess.job_id), None
            )
            if latest:
                daemon_id = latest.daemon_id
            delete_targets.add((sess.provider, daemon_id, cwd))

    results: list[dict] = []
    for provider, daemon_id, cwd in sorted(
        delete_targets, key=lambda item: (item[0], item[1] or "", item[2] or "")
    ):
        if daemon_id and _is_local_daemon_target(request, daemon_id):
            try:
                result = await _delete_local_session(provider, session_id, cwd)
            except RuntimeError as exc:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail=f"failed to delete provider transcript: {exc}",
                )
        else:
            daemon_links = getattr(request.app.state, "daemon_links", {})
            link = daemon_links.get(daemon_id) if daemon_id else None
            if link is None or not link.connected:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="owning daemon is unavailable for session deletion",
                )
            try:
                result = await link.delete_session(session_id, provider, cwd)
            except (WSRPCUnavailable, WSRPCUncertain):
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="daemon offline during session deletion",
                )
            except RPCRejected as exc:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=f"daemon rejected session deletion: {exc.body}",
                )

        if result.get("not_implemented"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    result.get("reason")
                    or f"provider session deletion is not implemented: {provider}"
                ),
            )
        if not result.get("ok"):
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="daemon could not delete provider session",
            )
        results.append(result)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    for task in tasks:
        task.session_id = None
        task.updated_at = now
    for sess in sessions:
        await db.delete(sess)
    await db.commit()

    return {
        "ok": True,
        "session_id": session_id,
        "session_rows_deleted": len(sessions),
        "task_pointers_cleared": len(tasks),
        "provider_results": results,
    }


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SessionHistoryOut:
    """Fetch a session transcript from its owning daemon.

    Remote daemons are queried with a ``session.history`` RPC over their
    reverse-dial link. A daemon configured with a local URL instead falls
    back to reading this machine's provider transcript directly, so HTTP
    long-poll local daemons do not need reverse-dial solely to echo history.

    Error mapping: 404 = unknown session id; 503 = no owning daemon / link
    offline; 502 = daemon answered but rejected the history RPC.
    """
    sess = (
        await db.execute(
            select(SessionModel)
            .where(SessionModel.session_id == session_id)
            .order_by(SessionModel.updated_at.desc())
        )
    ).scalars().first()
    if sess is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no session for id"
        )

    latest_step = (
        await db.execute(
            select(TaskModel)
            .where(TaskModel.job_id == sess.job_id)
            .order_by(TaskModel.updated_at.desc())
        )
    ).scalars().first()
    provider = latest_step.provider if latest_step else sess.provider
    daemon_id = latest_step.daemon_id if latest_step else None

    if not daemon_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="session has no owning daemon",
        )
    daemon_links = getattr(request.app.state, "daemon_links", {})
    link = daemon_links.get(daemon_id)
    if link is None or not link.connected:
        if _is_local_daemon_target(request, daemon_id):
            result = await _read_local_session_history(session_id, provider)
            return SessionHistoryOut(
                session_id=session_id,
                messages=result.get("messages", []),
                not_implemented=bool(result.get("not_implemented", False)),
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="daemon offline",
        )

    try:
        result = await link.get_session_history(session_id, provider)
    except (WSRPCUnavailable, WSRPCUncertain):
        if _is_local_daemon_target(request, daemon_id):
            result = await _read_local_session_history(session_id, provider)
            return SessionHistoryOut(
                session_id=session_id,
                messages=result.get("messages", []),
                not_implemented=bool(result.get("not_implemented", False)),
            )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="daemon offline",
        )
    except RPCRejected as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"daemon rejected history: {exc.body}",
        )

    return SessionHistoryOut(
        session_id=session_id,
        messages=result.get("messages", []),
        not_implemented=bool(result.get("not_implemented", False)),
    )


__all__ = ["router"]
