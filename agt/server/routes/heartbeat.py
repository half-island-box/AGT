"""Heartbeat and health-check routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import HeartbeatRequest, HeartbeatResponse, HealthResponse
from ..services.runtime_service import upsert_daemon_runtimes

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(status="ok")


@router.post("/heartbeat", response_model=HeartbeatResponse)
async def heartbeat(
    payload: HeartbeatRequest,
    db: AsyncSession = Depends(get_db),
) -> HeartbeatResponse:
    """Daemon heartbeat — syncs runtimes and stamps ``last_seen``.

    The daemon re-pushes its current probe snapshot on every heartbeat;
    the server replaces this daemon's rows (an empty snapshot removes
    them) and stamps ``last_seen`` on the survivors. This is the
    steady-state sync for local HTTP daemons; reverse-dial daemons send
    the same snapshot over WebSocket via the ``runtimes.register`` RPC.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await upsert_daemon_runtimes(
        db,
        daemon_id=payload.daemon_id,
        runtimes=[rt.model_dump() for rt in payload.runtimes],
        daemon_name=payload.daemon_name,
    )
    return HeartbeatResponse(ok=True, server_time=now)


__all__ = ["router"]