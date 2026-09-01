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

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain
from ..database import get_db
from ..models import JobModel, SessionModel, TaskModel
from ..schemas import SessionHistoryOut, SessionResolveOut

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


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


@router.get("/{session_id}/history")
async def get_session_history(
    session_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> SessionHistoryOut:
    """Fetch a remote session's transcript from its owning daemon.

    Backs the CLI's remote ``--resume`` history echo. The transcript lives
    on the daemon machine, so the Server resolves the session's ``daemon_id``
    and forwards a ``session.history`` RPC over that daemon's reverse-dial
    link — it never stores (or returns) transcripts it doesn't currently
    retrieve on demand.

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
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="daemon offline",
        )

    try:
        result = await link.get_session_history(session_id, provider)
    except (WSRPCUnavailable, WSRPCUncertain):
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
