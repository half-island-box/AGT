"""Session lookup route."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import SessionModel
from ..schemas import SessionOut

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("", response_model=SessionOut)
async def get_session(
    task_id: str = Query(..., min_length=1),
    provider: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
) -> SessionOut:
    """Return the session_id mapped to this task_id+provider, or 404."""
    rec = (
        await db.execute(
            select(SessionModel).where(
                SessionModel.task_id == task_id, SessionModel.provider == provider
            )
        )
    ).scalars().first()
    if rec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="no session for task"
        )
    return SessionOut(task_id=task_id, provider=provider, session_id=rec.session_id)


__all__ = ["router"]