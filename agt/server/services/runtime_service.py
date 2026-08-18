"""Runtime registry upsert shared by the HTTP and WS paths.

Both the HTTP ``POST /api/runtimes/register`` + ``POST /api/heartbeat``
and the reverse-dial WS ``runtimes.register`` RPC need the same
"replace this daemon's rows with the supplied snapshot" operation.
Keeping it in one function means the three entry points can't drift.

The snapshot element shape is ``{name, type, version, status,
device_name}`` — identical to what ``RuntimeInfo`` serializes to, and to
what the daemon builds in ``_heartbeat_loop``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RuntimeModel


async def upsert_daemon_runtimes(
    db: AsyncSession,
    *,
    daemon_id: str,
    runtimes: list[dict],
) -> int:
    """Replace ``daemon_id``'s rows with ``runtimes``; return the count.

    ``runtimes`` may be empty → the daemon's rows are removed (a daemon
    that uninstalled all its agents). Every inserted row gets a fresh
    ``last_seen`` so the registry doubles as the liveness signal.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        delete(RuntimeModel).where(RuntimeModel.daemon_id == daemon_id)
    )
    for rt in runtimes:
        db.add(
            RuntimeModel(
                daemon_id=daemon_id,
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


__all__ = ["upsert_daemon_runtimes"]
