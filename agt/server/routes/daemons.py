"""Daemon registry view routes (Phase 4 — daemon-level read model).

Daemons are aggregated from ``runtimes`` rows (no separate table). Unlike
``GET /api/runtimes`` — which filters stale rows so offline daemons vanish —
these endpoints read everything and carry the liveness signal in ``online``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import DaemonOut, IdentityBindingOut, ListBindingsResponse, ListDaemonsResponse
from ..services.binding_service import list_bindings_by_daemon
from ..services.daemon_service import get_daemon, list_daemons

router = APIRouter(prefix="/api/daemons", tags=["daemons"])


@router.get("", response_model=ListDaemonsResponse)
async def list_daemons_route(
    db: AsyncSession = Depends(get_db),
) -> ListDaemonsResponse:
    """List every known daemon (offline ones included, with heartbeat)."""
    daemons = await list_daemons(db)
    return ListDaemonsResponse(daemons=[DaemonOut(**d) for d in daemons])


@router.get("/{daemon_id}", response_model=DaemonOut)
async def get_daemon_route(
    daemon_id: str,
    db: AsyncSession = Depends(get_db),
) -> DaemonOut:
    """Return one daemon's aggregated details; 404 if it has no rows."""
    daemon = await get_daemon(daemon_id, db)
    if daemon is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"daemon not found: {daemon_id}",
        )
    return DaemonOut(**daemon)


@router.get("/{daemon_id}/bindings", response_model=ListBindingsResponse)
async def list_daemon_bindings_route(
    daemon_id: str,
    db: AsyncSession = Depends(get_db),
) -> ListBindingsResponse:
    """List every identity binding on a daemon, across all identities."""
    bindings = await list_bindings_by_daemon(daemon_id, db)
    return ListBindingsResponse(bindings=[IdentityBindingOut(**b) for b in bindings])


__all__ = ["router"]
