"""Lightweight schema migrations for SQLite (no Alembic).

``Base.metadata.create_all`` creates missing tables but never adds columns
to tables that already exist. When a model gains a column after the DB
file has been created, a tiny ``ALTER TABLE`` is needed so existing
deployments (``~/.agt/agt.db``) pick it up without manual deletion.

``ensure_schema`` is idempotent and must run **after** ``create_all`` so
the tables it inspects are guaranteed to exist.
"""

from __future__ import annotations

import logging

from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)


def _column_names(conn: Connection, table: str) -> set[str]:
    """Return the set of column names on ``table`` (SQLite PRAGMA)."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def ensure_schema(conn: Connection) -> None:
    """Add columns introduced after the initial schema. Idempotent."""
    # Step 3.6: daemon-targeted routing. A NULL target means "free task"
    # (any matching daemon may claim); a non-NULL target pins the task to
    # one daemon.
    if "target_daemon_id" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN target_daemon_id VARCHAR(64)"
        )
        log.info("Migrated tasks table: added target_daemon_id column")


__all__ = ["ensure_schema"]
