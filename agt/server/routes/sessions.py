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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import SessionModel

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def get_session(
    task_id: str = Query(..., min_length=1),
    provider: str | None = Query(None),
    identity_did: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Exact session lookup (provider given) or task-wide list (provider omitted)."""
    ctx = identity_did or ""

    if provider is not None:
        rec = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.task_id == task_id,
                    SessionModel.identity_did == ctx,
                    SessionModel.provider == provider,
                )
            )
        ).scalars().first()
        if rec is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="no session for task"
            )
        return {
            "task_id": task_id,
            "identity_did": rec.identity_did,
            "provider": provider,
            "session_id": rec.session_id,
        }

    recs = (
        await db.execute(
            select(SessionModel).where(SessionModel.task_id == task_id)
        )
    ).scalars().all()
    return {
        "sessions": [
            {
                "task_id": r.task_id,
                "identity_did": r.identity_did,
                "provider": r.provider,
                "session_id": r.session_id,
            }
            for r in recs
        ]
    }


__all__ = ["router"]
