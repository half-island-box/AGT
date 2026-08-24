"""Daemon registry aggregation — daemon-level view over runtimes (Phase 4).

Daemons have no own table; their existence lives entirely in the
``runtimes`` rows (one per registered provider). This service reads ALL
rows (no stale filter — offline daemons must stay visible), groups by
``daemon_id``, and derives a per-daemon summary: device, providers,
runtime count, latest heartbeat, online flag.

``online`` reuses ``runtime_service.is_stale`` so there is a single
liveness TTL definition in the codebase.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.models import RuntimeModel
from agt.server.services.runtime_service import is_stale

log = logging.getLogger(__name__)


def _provider_to_dict(r: RuntimeModel) -> dict[str, Any]:
    """One runtime row rendered as a provider entry inside a daemon."""
    return {
        "provider": r.provider,
        "name": r.name,
        "version": r.version,
        "status": r.status,
        "last_seen": r.last_seen,
    }


def _daemon_to_dict(rows: list[RuntimeModel]) -> dict[str, Any]:
    """Aggregate one daemon's rows (non-empty) into a summary dict."""
    # Latest heartbeat row wins for device_name / last_seen (design D2).
    latest = max(rows, key=lambda r: r.last_seen or "")
    providers = [_provider_to_dict(r) for r in rows]
    return {
        "daemon_id": latest.daemon_id,
        "daemon_name": latest.daemon_name,
        "device_name": latest.device_name,
        "providers": providers,
        "runtime_count": len(rows),
        "last_seen": latest.last_seen,
        "online": not is_stale(latest.last_seen),
    }


async def list_daemons(db: AsyncSession) -> list[dict[str, Any]]:
    """Return every known daemon, aggregated from runtimes rows.

    Rows are read WITHOUT the liveness filter so recently-offline daemons
    still appear (``online`` carries the liveness signal instead). Grouping
    is by daemon_id in insertion order of first appearance.
    """
    result = await db.execute(
        select(RuntimeModel).order_by(RuntimeModel.daemon_id, RuntimeModel.provider)
    )
    rows = result.scalars().all()

    grouped: dict[str, list[RuntimeModel]] = {}
    for r in rows:
        grouped.setdefault(r.daemon_id, []).append(r)

    return [_daemon_to_dict(rows) for rows in grouped.values()]


async def get_daemon(
    daemon_id: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Return one daemon's aggregated summary, or None if it has no rows."""
    result = await db.execute(
        select(RuntimeModel)
        .where(RuntimeModel.daemon_id == daemon_id)
        .order_by(RuntimeModel.provider)
    )
    rows = list(result.scalars().all())
    if not rows:
        return None
    return _daemon_to_dict(rows)


__all__ = ["list_daemons", "get_daemon"]
