"""Runtime registry upsert shared by the HTTP and WS paths.

Both the HTTP ``POST /api/runtimes/register`` + ``POST /api/heartbeat``
and the reverse-dial WS ``runtimes.register`` RPC need the same
"replace this daemon's rows with the supplied snapshot" operation.
Keeping it in one function means the three entry points can't drift.

The snapshot element shape is ``{name, type, version, status,
device_name}`` — identical to what ``RuntimeInfo`` serializes to, and to
what the daemon builds in ``_heartbeat_loop``.

Liveness: ``last_seen`` is the only liveness signal we have, so this
module owns the helpers that interpret it (``is_stale`` /
``prune_stale_runtimes``). The same predicate is reused by the runtime
listing endpoint and the targeted-task submission check so they can't
drift.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RuntimeModel

TS_FMT = "%Y-%m-%d %H:%M:%S"
RUNTIME_STALE_SECONDS = 90  # 3 × HEARTBEAT_INTERVAL(30s) — tolerate 1~2 lost beats


def _utcnow_naive() -> datetime:
    # ``last_seen`` is stored as a naive UTC string (see ``upsert_daemon_runtimes``
    # below). Comparing naive vs aware ``datetime`` raises; we strip tzinfo here
    # so the predicate is safe to call with a naive ``now`` argument.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def is_stale(
    last_seen: str,
    *,
    now: datetime | None = None,
    ttl: int = RUNTIME_STALE_SECONDS,
) -> bool:
    """Return True if ``last_seen`` is older than ``ttl`` seconds.

    Treats an empty / unparseable ``last_seen`` as stale — it means the
    row was migrated without a timestamp and we have no proof the daemon
    is alive.
    """
    if not last_seen:
        return True
    try:
        ts = datetime.strptime(last_seen, TS_FMT)
    except ValueError:
        return True
    now = now or _utcnow_naive()
    return (now - ts) > timedelta(seconds=ttl)


async def prune_stale_runtimes(
    db: AsyncSession,
    *,
    ttl: int = RUNTIME_STALE_SECONDS,
) -> int:
    """Delete rows whose ``last_seen`` is older than ``ttl`` seconds.

    Returns the number of rows removed. Called at server startup to
    sweep out remnants from daemons that crashed without deregistering
    (or whose process was never seen again). Cheap on small registries;
    if the table grows we can add an index on ``last_seen``.
    """
    rows = (await db.execute(select(RuntimeModel))).scalars().all()
    stale_ids = [r.id for r in rows if is_stale(r.last_seen, ttl=ttl)]
    if stale_ids:
        await db.execute(
            delete(RuntimeModel).where(RuntimeModel.id.in_(stale_ids))
        )
        await db.commit()
    return len(stale_ids)


async def upsert_daemon_runtimes(
    db: AsyncSession,
    *,
    daemon_id: str,
    runtimes: list[dict],
    daemon_name: str | None = None,
) -> int:
    """Replace ``daemon_id``'s rows with ``runtimes``; return the count.

    ``runtimes`` may be empty → the daemon's rows are removed (a daemon
    that uninstalled all its agents). Every inserted row gets a fresh
    ``last_seen`` so the registry doubles as the liveness signal.

    ``daemon_name`` (Phase 4): the daemon's human-readable label, stored
    on each runtime row alongside the stable ``daemon_id``. ``None`` keeps
    backward compatibility (legacy daemons that don't send it).
    """
    now = datetime.now(timezone.utc).strftime(TS_FMT)
    # Preserve the daemon's stored label when this upsert doesn't carry
    # one (legacy heartbeat path): the delete+reinsert below would
    # otherwise NULL it out on every 30s beat, wiping what register
    # wrote. A non-None ``daemon_name`` always wins (rename flow).
    effective_name = daemon_name
    if effective_name is None:
        effective_name = (
            await db.execute(
                select(RuntimeModel.daemon_name)
                .where(RuntimeModel.daemon_id == daemon_id)
                .limit(1)
            )
        ).scalar_one_or_none()
    await db.execute(
        delete(RuntimeModel).where(RuntimeModel.daemon_id == daemon_id)
    )
    for rt in runtimes:
        db.add(
            RuntimeModel(
                daemon_id=daemon_id,
                daemon_name=effective_name,
                provider=rt["type"],
                name=rt["name"],
                version=rt["version"],
                status=rt.get("status", "online"),
                device_name=rt["device_name"],
                last_seen=now,
            )
        )
    await db.commit()
    return len(runtimes)


__all__ = [
    "TS_FMT",
    "RUNTIME_STALE_SECONDS",
    "is_stale",
    "prune_stale_runtimes",
    "upsert_daemon_runtimes",
]
