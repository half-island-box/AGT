"""Manual migration of legacy Phase 1 sessions.json into SQLite.

The Server no longer invokes this automatically. Server-managed
chat/SOP/Goal sessions live only in SQLite; local direct chat uses the
per-cwd ``<cwd>/.agt/sessions.json`` store.
"""

from __future__ import annotations

from datetime import datetime, timezone

import json
import logging
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import SessionModel

log = logging.getLogger(__name__)


async def migrate_sessions_json(
    db: AsyncSession,
    sessions_json_path: str,
) -> int:
    """If sessions table is empty and the JSON file has records, copy them.

    Returns the number of records migrated.
    """
    json_path = Path(sessions_json_path).expanduser()
    if not json_path.exists():
        return 0

    # Only migrate if the table is currently empty.
    count = (await db.execute(select(func.count()).select_from(SessionModel))).scalar_one()
    if count and count > 0:
        return 0

    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Could not read sessions.json for migration: %s", exc)
        return 0

    records = data.get("records", {}) if isinstance(data, dict) else {}
    if not records:
        return 0

    migrated = 0
    for task_id, rec in records.items():
        if not isinstance(rec, dict):
            continue
        session_id = rec.get("session_id")
        provider = rec.get("provider")
        created_at = rec.get("created_at")
        if not (session_id and provider):
            continue
        try:
            created_at_val = (
                datetime.fromtimestamp(float(created_at), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                if created_at is not None
                else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            )
        except (TypeError, ValueError, OSError):
            created_at_val = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        db.add(
            SessionModel(
                task_id=str(task_id),
                provider=str(provider),
                session_id=str(session_id),
                created_at=created_at_val,
                # Phase 4: legacy JSON has no identity — the empty-string
                # sentinel marks these as identity-less sessions.
                identity_did="",
                updated_at=created_at_val,
            )
        )
        migrated += 1

    if migrated:
        await db.commit()
        log.info("Migrated %d session record(s) from %s", migrated, json_path)

    return migrated


__all__ = ["migrate_sessions_json"]
