"""Daemon registry view routes (Phase 4 — daemon-level read model).

Daemons are aggregated from ``runtimes`` rows (no separate table). Unlike
``GET /api/runtimes`` — which filters stale rows so offline daemons vanish —
these endpoints read everything and carry the liveness signal in ``online``.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import StepModel
from ..schemas import (
    DaemonOut,
    DaemonSessionOut,
    IdentityBindingOut,
    ListBindingsResponse,
    ListDaemonSessionsResponse,
    ListDaemonsResponse,
)
from ..services.artifact_service import artifacts_for_daemon
from ..services.binding_service import list_bindings_by_daemon
from ..services.daemon_service import get_daemon, list_daemons

router = APIRouter(prefix="/api/daemons", tags=["daemons"])


def _daemon_defs(request: Request) -> dict[str, object]:
    """Build the ``daemon_id → DaemonTarget`` lookup from the config mount.

    ``app.state.config`` is set at app-construction time; routes built
    without it (tests) fall back to an empty map — the aggregated view
    then only lists runtime-known daemons, with no url/capabilities overlay.
    """
    cfg = getattr(request.app.state, "config", None)
    return {t.daemon_id: t for t in (cfg.daemons if cfg else [])}


def _daemon_links(request: Request) -> dict[str, object]:
    """Build the ``daemon_id → DaemonLink`` lookup for the ``online`` signal.

    ``app.state.daemon_links`` is created in the server lifespan; without it
    (tests, or routes hit before startup) fall back to an empty map — the
    ``online`` flag then relies on heartbeat liveness alone.
    """
    return getattr(request.app.state, "daemon_links", None) or {}


@router.get("", response_model=ListDaemonsResponse)
async def list_daemons_route(    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ListDaemonsResponse:
    """List every known daemon (offline ones included, with heartbeat)."""
    daemons = await list_daemons(
        db,
        daemon_defs=_daemon_defs(request),
        daemon_links=_daemon_links(request),
    )
    return ListDaemonsResponse(daemons=[DaemonOut(**d) for d in daemons])


@router.get("/{daemon_id}", response_model=DaemonOut)
async def get_daemon_route(
    daemon_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> DaemonOut:
    """Return one daemon's aggregated details; 404 if it is unknown."""
    daemon = await get_daemon(
        daemon_id,
        db,
        daemon_defs=_daemon_defs(request),
        daemon_links=_daemon_links(request),
    )
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


@router.get("/{daemon_id}/sessions")
async def list_daemon_sessions_route(
    daemon_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> ListDaemonSessionsResponse:
    """List every conversation session this daemon has executed.

    Backs ``agt chat --list --daemon=X``. Unlike the per-cwd local store,
    this is a global, cross-directory view: each row is a task that ran on
    ``daemon_id`` and produced a ``session_id``. ``dir`` (agent_cwd, else
    work_dir) is the directory the session actually ran in.

    ``daemon_id`` (not ``target_daemon_id``) is the filter — it is set at
    claim time, so only tasks the daemon really executed qualify. Sessions
    are deduplicated by ``session_id`` as a defense (1 task == 1 session in
    practice).
    """
    rows = (
        await db.execute(
            select(StepModel)
            .where(
                StepModel.daemon_id == daemon_id,
                StepModel.session_id.isnot(None),
            )
            .order_by(StepModel.updated_at.desc())
            .limit(limit)
        )
    ).scalars().all()

    seen: set[str] = set()
    sessions: list[DaemonSessionOut] = []
    for t in rows:
        sid = t.session_id
        if sid in seen:
            continue
        seen.add(sid)
        sessions.append(
            DaemonSessionOut(
                session_id=sid,
                task_id=t.step_id,
                provider=t.provider,
                identity_did=t.identity_did,
                dir=t.agent_cwd or t.work_dir,
                first_prompt=t.prompt,
                last_seen_at=t.updated_at,
            )
        )
    return ListDaemonSessionsResponse(sessions=sessions)


@router.get("/{daemon_id}/artifacts")
async def list_daemon_artifacts_route(
    daemon_id: str,
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    """List every artifact the daemon produced, each with its ``session_id``.

    Backs ``agt artifact list --daemon X`` (no ``--session``) — the
    cross-session view. Each row carries the standard public artifact
    metadata plus the producing session's id.
    """
    return await artifacts_for_daemon(db, daemon_id)


@router.post("/refresh")
async def refresh_daemons_route(
    request: Request,
    daemon_id: str | None = Query(
        None,
        description=(
            "Narrow the forced re-dial to this target. The registry itself "
            "is always re-read from the daemons file (it is global)."
        ),
    ),
) -> dict:
    """Re-read ``~/.agt/daemons`` and reconcile the live reverse-dial links.

    A daemon added while the Server was running used to need a restart
    because the config snapshot was built once. This endpoint applies the
    file immediately: new targets are dialed, dropped ones are stopped, and
    a disconnected target is re-dialed now instead of waiting out its
    reconnect backoff. Healthy links are left untouched, so in-flight
    claims/reports are never interrupted.

    Returns one row per configured target:
    ``{daemon_id, action, connected}`` with ``action`` ∈ ``added`` /
    ``removed`` / ``replaced`` / ``redialled`` / ``unchanged``.
    """
    pool = getattr(request.app.state, "daemon_link_pool", None)
    if pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "type": "daemon_pool_unavailable",
                "message": "server did not start a daemon link pool",
            },
        )
    return {
        "daemons": await pool.refresh(daemon_id=daemon_id),
        "daemons_path": pool.daemons_path,
    }


__all__ = ["router"]
