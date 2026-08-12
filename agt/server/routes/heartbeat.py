"""Heartbeat and health-check routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import RuntimeModel
from ..schemas import HeartbeatRequest, HeartbeatResponse, HealthResponse

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    payload: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    """Daemon heartbeat — refreshes last_seen on all runtimes for this daemon."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        update(RuntimeModel)
        .where(RuntimeModel.daemon_id == payload.daemon_id)
        .values(last_seen=now)
    )
    await db.commit()
    return HeartbeatResponse(ok=True, server_time=now)


__all__ = ["router"]