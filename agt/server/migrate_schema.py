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


def _table_exists(conn: Connection, table: str) -> bool:
    """Return True when ``table`` already exists in the database."""
    rows = conn.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchall()
    return bool(rows)


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


_SESSIONS_PK = ["job_id", "identity_did", "provider"]


def _rebuild_sessions_table(conn: Connection) -> None:
    """Rebuild ``sessions`` with the Job-based composite PK, preserving data.

    SQLite cannot alter a PRIMARY KEY in place, so we create a new table,
    copy every row across (stamping ``identity_did=''`` for legacy rows,
    ``updated_at=created_at``, and resolving ``task_id → job_id`` via the
    ``tasks`` table), then swap it in. Idempotent: a no-op when the table
    already has the new PK (or doesn't exist yet).
    """
    if _table_pk(conn, "sessions") in ([], _SESSIONS_PK):
        return

    # Determine which columns the old sessions table has.
    old_cols = _column_names(conn, "sessions")
    has_identity_did = "identity_did" in old_cols
    has_updated_at = "updated_at" in old_cols

    conn.exec_driver_sql("""
        CREATE TABLE sessions_new (
            job_id TEXT NOT NULL,
            identity_did TEXT NOT NULL DEFAULT '',
            provider TEXT NOT NULL,
            session_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (job_id, identity_did, provider),
            FOREIGN KEY (job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
        )
    """)

    identity_expr = "s.identity_did" if has_identity_did else "''"
    updated_at_expr = "s.updated_at" if has_updated_at else "s.created_at"

    conn.exec_driver_sql(f"""
        INSERT INTO sessions_new
            (job_id, identity_did, provider, session_id, created_at, updated_at)
        SELECT t.job_id, {identity_expr}, s.provider, s.session_id, s.created_at, {updated_at_expr}
        FROM sessions s
        JOIN tasks t ON t.task_id = s.task_id
    """)
    conn.exec_driver_sql("DROP TABLE sessions")
    conn.exec_driver_sql("ALTER TABLE sessions_new RENAME TO sessions")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_sessions_job ON sessions(job_id)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_sessions_session ON sessions(session_id)"
    )
    log.info("Rebuilt sessions table with (job_id, identity_did, provider) PK")


def ensure_schema(conn: Connection) -> None:
    """Add columns introduced after the initial schema. Idempotent."""
    # Step 3.6: daemon-targeted routing. A NULL target means "free task"
    # (any matching daemon may claim); a non-NULL target pins the task to
    # one daemon.
    if _table_exists(conn, "tasks") and "target_daemon_id" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN target_daemon_id VARCHAR(64)"
        )
        log.info("Migrated tasks table: added target_daemon_id column")

    # Phase 4 workdir: AGT metadata directory for the task, derived from
    # the user's cwd at submit time. NULL for legacy tasks (daemon falls
    # back to work_dir_root / provider / task_id) and for remote-daemon
    # tasks (the receiving daemon supplies its own fallback path).
    if _table_exists(conn, "tasks") and "work_dir" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN work_dir VARCHAR(512)"
        )
        log.info("Migrated tasks table: added work_dir column")

    # Remote chat parity: whether the agent refused to resume a prior
    # session on its last report. Exposed via task status so the CLI can
    # drop a stale cwd-store session entry.
    if _table_exists(conn, "tasks") and "resume_rejected" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN resume_rejected BOOLEAN"
        )
        log.info("Migrated tasks table: added resume_rejected column")

    # cwd injection (Phase 4 workdir supplement): the agent subprocess's
    # working directory on the daemon machine. Distinct from ``work_dir``
    # (which is AGT metadata). NULL = daemon-side default fallback.
    if _table_exists(conn, "tasks") and "agent_cwd" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN agent_cwd VARCHAR(1024)"
        )
        log.info("Migrated tasks table: added agent_cwd column")

    # Phase 4 daemon identity: human-readable label for a daemon, stored
    # on each runtime row alongside the stable ``daemon_id`` (uuid5 of the
    # daemon URL). NULL for legacy rows / daemons that don't send it.
    if _table_exists(conn, "runtimes") and "daemon_name" not in _column_names(conn, "runtimes"):
        conn.exec_driver_sql(
            "ALTER TABLE runtimes ADD COLUMN daemon_name VARCHAR(128)"
        )
        log.info("Migrated runtimes table: added daemon_name column")

    # Phase 2 artifact foundation: this Step's structured input as an
    # A2A-shaped Message JSON. New rows are always populated (normalized
    # from prompt-only submissions); legacy rows keep NULL. The
    # ``artifacts`` table itself is created by ``create_all``.
    if _table_exists(conn, "tasks") and "input_message" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN input_message TEXT"
        )
        log.info("Migrated tasks table: added input_message column")

    # Goal shared context: audit fields for the claim-time ContextBrief.
    # The table itself is created by Base.metadata.create_all; these ALTERs
    # cover databases whose tasks table already existed before this phase.
    for column, ddl in (
        ("context_brief_json", "TEXT"),
        ("context_snapshot_id", "VARCHAR(64)"),
        ("context_watermark", "INTEGER"),
    ):
        if _table_exists(conn, "tasks") and column not in _column_names(conn, "tasks"):
            conn.exec_driver_sql(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
            log.info("Migrated tasks table: added %s column", column)

    if _table_exists(conn, "context_entries"):
        conn.exec_driver_sql("""
            CREATE INDEX IF NOT EXISTS ix_context_entries_root_id
            ON context_entries(root_job_id, entry_id)
        """)
        conn.exec_driver_sql("""
            CREATE INDEX IF NOT EXISTS ix_context_entries_root_key
            ON context_entries(root_job_id, key, status)
        """)
        conn.exec_driver_sql("""
            CREATE INDEX IF NOT EXISTS ix_context_entries_root_author
            ON context_entries(root_job_id, author_job_id)
        """)
        conn.exec_driver_sql("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_context_entries_step_idempotency
            ON context_entries(author_step_id, idempotency_key)
        """)
        conn.exec_driver_sql("""
            CREATE UNIQUE INDEX IF NOT EXISTS ux_context_entries_active_key
            ON context_entries(root_job_id, key) WHERE status = 'active'
        """)

    # Phase 3: Job-tree lineage + spawn payload sidecar. ``root_job_id``
    # needs NOT NULL + backfill for existing depth-1 rows (a root's root
    # is itself). ``outcome_payload`` carries spawn intent envelopes.
    if _table_exists(conn, "jobs") and "parent_job_id" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN parent_job_id VARCHAR(64)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN root_job_id VARCHAR(64) NOT NULL DEFAULT ''"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_parent ON jobs (parent_job_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_root ON jobs (root_job_id)"
        )
        conn.exec_driver_sql(
            "UPDATE jobs SET root_job_id = job_id WHERE root_job_id = ''"
        )
        log.info("Migrated jobs table: added parent_job_id/root_job_id columns")

    if _table_exists(conn, "tasks") and "outcome_payload" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN outcome_payload TEXT"
        )
        log.info("Migrated tasks table: added outcome_payload column")

    # Phase 4: index for the routing load aggregate (GROUP BY daemon_id).
    # New databases get it from create_all; existing ones get it here.
    # Guarded on the column existing so a minimal legacy schema (e.g. the
    # migration test fixtures) that predates ``daemon_id`` doesn't break.
    if _table_exists(conn, "tasks") and "daemon_id" in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_tasks_daemon_id ON tasks (daemon_id)"
        )

    # Phase 5 (SOP): DAG dependency edges + per-node activation metadata.
    if not _table_exists(conn, "job_parents"):
        conn.exec_driver_sql("""
            CREATE TABLE job_parents (
                job_id VARCHAR(64) NOT NULL,
                parent_job_id VARCHAR(64) NOT NULL,
                created_at VARCHAR(32) NOT NULL,
                PRIMARY KEY (job_id, parent_job_id),
                FOREIGN KEY (job_id) REFERENCES jobs (job_id) ON DELETE CASCADE,
                FOREIGN KEY (parent_job_id) REFERENCES jobs (job_id) ON DELETE CASCADE
            )
        """)
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_job_parents_job ON job_parents (job_id)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_job_parents_parent ON job_parents (parent_job_id)"
        )
        log.info("Migrated schema: created job_parents table")

    if _table_exists(conn, "jobs") and "sop_node" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_node TEXT"
        )
        log.info("Migrated jobs table: added sop_node column")

    # Phase 5 loops: loop membership, iteration counter, and duplicated
    # loop-level metadata on each loop job.
    if _table_exists(conn, "jobs") and "sop_loop_name" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_loop_name VARCHAR(64)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_iteration INTEGER NOT NULL DEFAULT 0"
        )
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_loop_meta TEXT"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_sop_loop_name ON jobs (sop_loop_name)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_sop_iteration ON jobs (sop_iteration)"
        )
        log.info("Migrated jobs table: added sop loop columns")

    # Goal mode: root-Job metadata JSON (per-run spawn budget, template
    # fingerprint, raw goal text). NULL on non-goal jobs.
    if _table_exists(conn, "jobs") and "goal_meta" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN goal_meta TEXT"
        )
        log.info("Migrated jobs table: added goal_meta column")

    # Chat history title: preserve the immutable first user prompt on the
    # conversation owner (Job), rather than duplicating it on every provider
    # session pointer row.
    if _table_exists(conn, "jobs") and "first_prompt" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN first_prompt TEXT"
        )
        log.info("Migrated jobs table: added first_prompt column")

    jobs_columns = _column_names(conn, "jobs") if _table_exists(conn, "jobs") else set()
    tasks_columns = _column_names(conn, "tasks") if _table_exists(conn, "tasks") else set()
    if (
        {"job_id", "job_type", "first_prompt"} <= jobs_columns
        and {"job_id", "task_id", "prompt", "created_at"} <= tasks_columns
    ):
        conn.exec_driver_sql("""
            UPDATE jobs
            SET first_prompt = (
                SELECT t.prompt
                FROM tasks AS t
                WHERE t.job_id = jobs.job_id
                ORDER BY t.created_at ASC, t.task_id ASC
                LIMIT 1
            )
            WHERE job_type = 'chat'
              AND (first_prompt IS NULL OR first_prompt = '')
        """)

    # Chat attachments: user-uploaded inputs owned by the Server. New
    # databases get this from ``create_all``; this guard covers DB files
    # whose tables were created before the feature landed (and keeps the
    # Alembic / ensure_schema tracks in step).
    if not _table_exists(conn, "input_uploads"):
        conn.exec_driver_sql("""
            CREATE TABLE input_uploads (
                upload_id VARCHAR(64) NOT NULL,
                name VARCHAR(512) NOT NULL,
                mime_type VARCHAR(128) NOT NULL DEFAULT '',
                size INTEGER NOT NULL,
                sha256 VARCHAR(64) NOT NULL,
                storage_path VARCHAR(1024),
                inline_bytes BLOB,
                source VARCHAR(32) NOT NULL DEFAULT '',
                created_at VARCHAR(32) NOT NULL,
                expires_at VARCHAR(32),
                PRIMARY KEY (upload_id)
            )
        """)
        conn.exec_driver_sql(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_input_uploads_sha256 "
            "ON input_uploads (sha256)"
        )
        log.info("Migrated schema: created input_uploads table")

    # Structured agent events + per-turn token usage. New databases get
    # both from ``create_all``; these guards cover DB files that predate
    # the feature (the same dual-track pattern as the other Phase columns).
    for column, ddl in (
        ("input_tokens", "INTEGER"),
        ("output_tokens", "INTEGER"),
        ("cache_read_tokens", "INTEGER"),
        ("cache_creation_tokens", "INTEGER"),
        ("cost_usd", "REAL"),
        ("model", "VARCHAR(128)"),
        ("num_turns", "INTEGER"),
    ):
        if _table_exists(conn, "tasks") and column not in _column_names(conn, "tasks"):
            conn.exec_driver_sql(f"ALTER TABLE tasks ADD COLUMN {column} {ddl}")
            log.info("Migrated tasks table: added %s column", column)

    if not _table_exists(conn, "step_events"):
        conn.exec_driver_sql("""
            CREATE TABLE step_events (
                task_id VARCHAR(64) NOT NULL,
                seq INTEGER NOT NULL,
                kind VARCHAR(32) NOT NULL,
                text_offset INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                created_at VARCHAR(32) NOT NULL,
                PRIMARY KEY (task_id, seq),
                FOREIGN KEY (task_id) REFERENCES tasks (task_id) ON DELETE CASCADE
            )
        """)
        log.info("Migrated schema: created step_events table")

    # Phase 6: unify run history by cwd and first-class job_type.
    if _table_exists(conn, "jobs") and "cwd" not in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN cwd VARCHAR(1024)"
        )
        log.info("Migrated jobs table: added cwd column")

    # SOP run-template association. Legacy rows stay NULL so they are never
    # incorrectly attributed to a template.
    if (
        _table_exists(conn, "jobs")
        and "sop_template_path" not in _column_names(conn, "jobs")
    ):
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_template_path VARCHAR(1024)"
        )
        conn.exec_driver_sql(
            "ALTER TABLE jobs ADD COLUMN sop_template_hash VARCHAR(128)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_sop_template_path "
            "ON jobs (sop_template_path)"
        )
        log.info("Migrated jobs table: added SOP template association columns")

    if _table_exists(conn, "sessions") and "cwd" not in _column_names(conn, "sessions"):
        conn.exec_driver_sql(
            "ALTER TABLE sessions ADD COLUMN cwd VARCHAR(1024)"
        )
        log.info("Migrated sessions table: added cwd column")

    # Backfill cwd from the best available directory column.
    if (
        _table_exists(conn, "jobs")
        and "cwd" in _column_names(conn, "jobs")
        and "work_dir" in _column_names(conn, "jobs")
    ):
        conn.exec_driver_sql(
            "UPDATE jobs SET cwd = COALESCE(work_dir, agent_cwd) WHERE cwd IS NULL"
        )
    if (
        _table_exists(conn, "jobs")
        and "job_id" in _column_names(conn, "sessions")
        and "cwd" in _column_names(conn, "sessions")
    ):
        conn.exec_driver_sql("""
            UPDATE sessions
            SET cwd = (SELECT j.cwd FROM jobs j WHERE j.job_id = sessions.job_id)
            WHERE cwd IS NULL
        """)

    # job_type widened to {"chat","sop","goal","task"}. SQLite stores it
    # as a plain string, so we only need to backfill legacy SOP/Goal rows.
    if _table_exists(conn, "jobs") and "job_type" in _column_names(conn, "jobs"):
        conn.exec_driver_sql(
            "UPDATE jobs SET job_type = 'goal' WHERE goal_meta IS NOT NULL AND job_type != 'goal'"
        )
        conn.exec_driver_sql(
            "UPDATE jobs SET job_type = 'sop' WHERE sop_node IS NOT NULL AND goal_meta IS NULL AND job_type != 'sop'"
        )

    # Phase 6 indexes for the unified /api/runs queries.
    if _table_exists(conn, "jobs"):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_cwd ON jobs (cwd)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_cwd_job_type_updated_at ON jobs (cwd, job_type, updated_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_cwd_root_job_id_updated_at ON jobs (cwd, root_job_id, updated_at)"
        )
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_jobs_sop_template_path "
            "ON jobs (sop_template_path)"
        )
    if _table_exists(conn, "sessions"):
        conn.exec_driver_sql(
            "CREATE INDEX IF NOT EXISTS ix_sessions_cwd ON sessions (cwd)"
        )


def migrate_identities_and_sessions(conn: Connection, root_dir: Path | None = None) -> None:
    """Apply Phase 4 identity + Job/Step + session schema migrations. Idempotent.

    Creates:
    - identities table (did PK, daemon_id, name, description, capabilities, status, visibility, created_by, timestamps)
    - identity_bindings table (m:n relationship for Step 2, created now for schema)
    - jobs table (job_id PK, status, provider, identity_did, target_daemon_id, work_dir, agent_cwd, timestamps)
    - tasks.job_id column (NULLable FK to jobs, indexed)
    - tasks.outcome column (NULLable JSON/text)
    - sessions table rebuilt with the (job_id, identity_did, provider) PK

    Args:
        conn: SQLAlchemy connection
        root_dir: Optional root directory for AGENTS.md files (typically ~/.agt)
    """
    root_dir = root_dir or Path.home()

    # Bring in any Phase 4 columns (target_daemon_id, work_dir, agent_cwd,
    # resume_rejected, daemon_name) that may be missing on legacy DBs, so the
    # backfill below can safely read them.
    ensure_schema(conn)

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

    # 3. Create jobs table (Phase 1 Job/Step split)
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'queued',
            job_type TEXT NOT NULL DEFAULT 'task',
            provider TEXT NOT NULL,
            identity_did TEXT,
            target_daemon_id TEXT,
            work_dir TEXT,
            agent_cwd TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_jobs_job_type ON jobs(job_type)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_jobs_provider ON jobs(provider)"
    )
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_jobs_identity ON jobs(identity_did)"
    )
    log.info("Idempotent: jobs table and indexes ready")

    # 4. Add identity_did, job_id, outcome columns to tasks table
    if "identity_did" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN identity_did TEXT DEFAULT NULL"
        )
        log.info("Migrated tasks table: added identity_did column")
    else:
        log.info("Tasks table already has identity_did column")

    if "job_id" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN job_id TEXT DEFAULT NULL"
        )
        log.info("Migrated tasks table: added job_id column")
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tasks_job ON tasks(job_id)"
    )

    if "outcome" not in _column_names(conn, "tasks"):
        conn.exec_driver_sql(
            "ALTER TABLE tasks ADD COLUMN outcome TEXT DEFAULT NULL"
        )
        log.info("Migrated tasks table: added outcome column")

    # The index must exist on BOTH paths: legacy DBs (column just added)
    # and fresh DBs where create_all already created the column — in the
    # fresh path the ALTER above is skipped, so create it unconditionally.
    conn.exec_driver_sql(
        "CREATE INDEX IF NOT EXISTS idx_tasks_identity ON tasks(identity_did)"
    )

    # 5. Backfill jobs for legacy tasks that don't have a job_id yet.
    #    Each legacy task becomes a single-step job; job_id == task_id keeps
    #    the migration simple and preserves any external references.
    cursor = conn.exec_driver_sql(
        "SELECT COUNT(*) FROM tasks WHERE job_id IS NULL"
    )
    missing_jobs = cursor.fetchone()[0]
    if missing_jobs:
        conn.exec_driver_sql("""
            INSERT OR IGNORE INTO jobs (job_id, status, job_type, provider, identity_did, target_daemon_id, work_dir, agent_cwd, created_at, updated_at)
            SELECT task_id, status, 'task', provider, identity_did, target_daemon_id, work_dir, agent_cwd, created_at, updated_at
            FROM tasks
            WHERE job_id IS NULL
        """)
        conn.exec_driver_sql("""
            UPDATE tasks SET job_id = task_id WHERE job_id IS NULL
        """)
        log.info("Migrated %d legacy tasks into Job + Step pairs", missing_jobs)

    # 6. Rebuild sessions with the composite (job_id, identity_did,
    #    provider) PK (Phase 1 Job/Step split). No-op on fresh DBs (create_all
    #    already produced the new schema) and on already-migrated DBs.
    _rebuild_sessions_table(conn)

    # 7. Ensure ~/.agt/identities directory exists (for AGENTS.md files)
    identities_dir = root_dir / ".agt" / "identities"
    identities_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"Idempotent: ensured identities directory exists at {identities_dir}")


__all__ = ["ensure_schema", "migrate_identities_and_sessions"]
