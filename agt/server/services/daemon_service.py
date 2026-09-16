"""Daemon registry aggregation — daemon-level view over runtimes (Phase 4).

Daemons have no own table; their existence lives entirely in the
``runtimes`` rows (one per registered provider). This service reads ALL
rows (no stale filter — offline daemons must stay visible), groups by
``daemon_id``, and derives a per-daemon summary: device, providers,
runtime count, latest heartbeat, online flag.

The ``~/.agt/daemons`` config file is the *base* list: every configured
target appears in the listing even if it has never registered a runtime
(``online=False``, no heartbeat, ``runtime_count=0``), and its static
``url`` / ``capabilities`` / display-name overlay is merged on top.
Daemons that only exist in ``runtimes`` (e.g. local HTTP long-poll
daemons with no file entry) are appended after the configured ones.

``online`` is true when either (a) the reverse-dial ``DaemonLink`` is
currently connected, or (b) the latest runtime heartbeat is fresh. The
heartbeat TTL reuses ``runtime_service.is_stale`` so there is a single
liveness definition in the codebase.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

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


def _is_online(link: Any | None, last_seen: str | None) -> bool:
    """A daemon is online if its reverse-dial link is up, or its latest
    heartbeat is fresh. ``last_seen=None`` (never seen) → offline unless
    a live link proves otherwise."""
    if link is not None and getattr(link, "connected", False):
        return True
    return not is_stale(last_seen or "")


def _build_daemon(
    daemon_id: str,
    rows: list[RuntimeModel],
    target: Any | None,
    link: Any | None,
) -> dict[str, Any]:
    """Merge runtime rows (if any) with a config target (if any).

    ``target`` is a ``DaemonTarget``-shaped object (or ``None`` for a
    runtime-only daemon); ``link`` is a ``DaemonLink``-shaped object whose
    ``connected`` state feeds the ``online`` flag.
    """
    if rows:
        # Latest heartbeat row wins for device_name / daemon_name / last_seen.
        latest = max(rows, key=lambda r: r.last_seen or "")
        providers = [_provider_to_dict(r) for r in rows]
        device_name = latest.device_name
        daemon_name = latest.daemon_name
        last_seen = latest.last_seen
    else:
        providers = []
        device_name = None
        daemon_name = None
        last_seen = None

    url: str | None = None
    capabilities: list[str] = []
    if target is not None:
        url = getattr(target, "url", None)
        capabilities = list(getattr(target, "capabilities", ()) or ())
        if not daemon_name:
            daemon_name = getattr(target, "name", None)

    d: dict[str, Any] = {
        "daemon_id": daemon_id,
        "daemon_name": daemon_name,
        "device_name": device_name,
        "providers": providers,
        "runtime_count": len(rows),
        "last_seen": last_seen,
        "online": _is_online(link, last_seen),
    }
    if target is not None:
        d["url"] = url
        d["capabilities"] = capabilities
    return d


async def list_daemons(
    db: AsyncSession,
    daemon_defs: Mapping[str, Any] | None = None,
    daemon_links: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return every known daemon: configured targets first, then runtime-only.

    ``daemon_defs`` (keyed by daemon_id) supplies the configured targets
    and their static ``url``/``capabilities``/name overlay; its insertion
    order is the listing order. ``daemon_links`` (keyed by daemon_id)
    supplies the reverse-dial connection objects whose ``connected`` state
    feeds the ``online`` flag alongside the heartbeat.
    """
    result = await db.execute(
        select(RuntimeModel).order_by(RuntimeModel.daemon_id, RuntimeModel.provider)
    )
    rows = result.scalars().all()

    grouped: dict[str, list[RuntimeModel]] = {}
    for r in rows:
        grouped.setdefault(r.daemon_id, []).append(r)

    targets = daemon_defs or {}
    links = daemon_links or {}

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    # Configured daemons first, in file order — even if never heartbeated.
    for daemon_id, target in targets.items():
        out.append(
            _build_daemon(
                daemon_id, grouped.get(daemon_id, []), target, links.get(daemon_id)
            )
        )
        seen.add(daemon_id)
    # Runtime-only daemons (no file entry) appended after.
    for daemon_id, grp in grouped.items():
        if daemon_id in seen:
            continue
        out.append(_build_daemon(daemon_id, grp, None, links.get(daemon_id)))
    return out


async def get_daemon(
    daemon_id: str,
    db: AsyncSession,
    daemon_defs: Mapping[str, Any] | None = None,
    daemon_links: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Return one daemon's aggregated summary, or None if it is unknown
    (no runtime rows AND no config target)."""
    result = await db.execute(
        select(RuntimeModel)
        .where(RuntimeModel.daemon_id == daemon_id)
        .order_by(RuntimeModel.provider)
    )
    rows = list(result.scalars().all())
    targets = daemon_defs or {}
    target = targets.get(daemon_id)
    if not rows and target is None:
        return None
    links = daemon_links or {}
    return _build_daemon(daemon_id, rows, target, links.get(daemon_id))


__all__ = ["list_daemons", "get_daemon"]
