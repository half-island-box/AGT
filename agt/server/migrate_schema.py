"""Lightweight schema migrations for SQLite (no Alembic).

``Base.metadata.create_all`` creates missing tables but never adds columns
to tables that already exist. When a model gains a column after the DB
file has been created, a tiny ``ALTER TABLE`` is needed so existing
deployments (``~/.agt/agt.db``) pick it up without manual deletion.

``ensure_schema`` is idempotent and must run **after** ``create_all`` so
the tables it inspects are guaranteed to exist.
"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy.engine import Connection

log = logging.getLogger(__name__)


def _column_names(conn: Connection, table: str) -> set[str]:
    """Return the set of column names on ``table`` (SQLite PRAGMA)."""
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def _table_pk(conn: Connection, table: str) -> list[str]:
    """Return the PK column names of ``table`` in declaration order.

    ``PRAGMA table_info`` reports the PK position in the last column
    (1-based, 0 for non-PK). An empty result means the table doesn't
    exist yet — ``create_all`` will build it with the model schema.
    """
    rows = conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()
    pk_rows = [r for r in rows if r[5] > 0]
    pk_rows.sort(key=lambda r: r[5])
    return [r[1] for r in pk_rows]


_SESSIONS_PK = ["task_id", "identity_did", "provider"]


def _rebuild_sessions_table(conn: Connection) -> None:
    """Rebuild ``sessions`` with the Phase 4 composite PK, preserving data.

    SQLite cannot alter a PRIMARY KEY in place, so we create a new table,
    copy every row across (stamping ``identity_did=''`` for legacy rows
    and ``updated_at=created_at``), then swap it in. Idempotent: a no-op
    when the table already has the new PK (or doesn't exist yet).
    """
    if _table_pk(conn, "sessions") in ([], _SESSIONS_PK):
        return
    conn.exec_driver_sql("""
        CREATE TABLE sessions_new (
            task_id TEXT NOT NULL,
            identity_did TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (task_id, identity_did, provider)
        )
    """)
    conn.exec_driver_sql("""
        INSERT INTO sessions_new
            (task_id, identity_did, provider, session_id, created_at, updated_at)
        SELECT task_id, '', provider, session_id, created_at, created_at
        FROM sessions
    """)
    conn.exec_driver_sql("DROP TABLE sessions")
    conn.exec_driver_sql("ALTER TABLE sessions_new RENAME TO sessions")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_sessions_task ON sessions(task_id)"
    )
    log.info("Rebuilt sessions table with (task_id, identity_did, provider) PK")


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

    # Phase 4 workdir: AGT metadata directory for the task, derived from
    # the user's cwd at submit time. NULL for legacy tasks (daemon falls
    # back to work_dir_root / provider / task_id) and for remote-daemon
    # tasks (the receiving daemon supplies its own fallback path).
    if "work_dir" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN work_dir VARCHAR(512)"
        )
        log.info("Migrated tasks table: added work_dir column")

    # Remote chat parity: whether the agent refused to resume a prior
    # session on its last report. Exposed via task status so the CLI can
    # drop a stale cwd-store session entry.
    if "resume_rejected" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN resume_rejected BOOLEAN"
        )
        log.info("Migrated tasks table: added resume_rejected column")

    # cwd injection (Phase 4 workdir supplement): the agent subprocess's
    # working directory on the daemon machine. Distinct from ``work_dir``
    # (which is AGT metadata). NULL = daemon-side default fallback.
    if "agent_cwd" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN agent_cwd VARCHAR(1024)"
        )
        log.info("Migrated tasks table: added agent_cwd column")

    # Phase 4 daemon identity: human-readable label for a daemon, stored
    # on each runtime row alongside the stable ``daemon_id`` (uuid5 of the
    # daemon URL). NULL for legacy rows / daemons that don't send it.
    if "daemon_name" not in _column_names(conn, "runtimes"):
        conn.exec_driver_sql(
            "ALTER TABLE runtimes ADD COLUMN daemon_name VARCHAR(128)"
        )
        log.info("Migrated runtimes table: added daemon_name column")


def migrate_identities_and_sessions(conn: Connection, root_dir: Path | None = None) -> None:
    """Apply Phase 4 identity + session schema migrations. Idempotent.

    Creates:
    - identities table (did PK, daemon_id, name, description, capabilities, status, visibility, created_by, timestamps)
    - identity_bindings table (m:n relationship for Step 2, created now for schema)
    - tasks.identity_did column (NULLable, indexed)
    - sessions table rebuilt with the (task_id, identity_did, provider) PK

    Args:
        conn: SQLAlchemy connection
        root_dir: Optional root directory for AGENTS.md files (typically ~/.agt)
    """
    root_dir = root_dir or Path.home()

    # 1. Create identities table
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS identities (
            did TEXT PRIMARY KEY,
            daemon_id TEXT NOT NULL,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            capabilities TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'active',
            visibility TEXT NOT NULL DEFAULT 'private',
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_identities_daemon ON identities(daemon_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_identities_status ON identities(status)"
    )
    log.info("Idempotent: identities table and indexes ready")

    # 2. Create identity_bindings table (for Step 2)
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS identity_bindings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            identity_did TEXT NOT NULL,
            daemon_id TEXT NOT NULL,
            provider TEXT NOT NULL,
            priority INTEGER NOT NULL DEFAULT 0,
            is_default INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            UNIQUE (identity_did, daemon_id, provider),
            FOREIGN KEY (identity_did) REFERENCES identities(did) ON DELETE CASCADE
        )
    """)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_bindings_identity ON identity_bindings(identity_did)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_bindings_runtime ON identity_bindings(daemon_id, provider)"
    )
    log.info("Idempotent: identity_bindings table and indexes ready")

    # 3. Add identity_did column to tasks table
    if "identity_did" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN identity_did TEXT DEFAULT NULL"
        )
        log.info("Migrated tasks table: added identity_did column")
    else:
        log.info("Tasks table already has identity_did column")
    # The index must exist on BOTH paths: legacy DBs (column just added)
    # and fresh DBs where create_all already created the column — in the
    # fresh path the ALTER above is skipped, so create it unconditionally.
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tasks_identity ON tasks(identity_did)"
    )

    # 4. Rebuild sessions with the composite (task_id, identity_did,
    #    provider) PK (Phase 4 Step 3). No-op on fresh DBs (create_all
    #    already produced the new schema) and on already-migrated DBs.
    _rebuild_sessions_table(conn)

    # 5. Ensure ~/.agt/identities directory exists (for AGENTS.md files)
    identities_dir = root_dir / ".agt" / "identities"
    identities_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Idempotent: ensured identities directory exists at {identities_dir}")


__all__ = ["ensure_schema", "migrate_identities_and_sessions"]
