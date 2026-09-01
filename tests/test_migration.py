"""Phase 4 Step 1 — schema migration tests.

``migrate_identities_and_sessions`` must be idempotent and safe on both
kinds of databases found in the wild:

- **Legacy DBs** (pre-Phase 4): a ``tasks`` table without ``identity_did``
  gets the column added via ALTER TABLE; existing rows survive untouched.
- **Fresh/repeat runs**: CREATE TABLE IF NOT EXISTS + PRAGMA column checks
  mean a second (or tenth) run is a no-op, never an error.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, text

from agt.server.migrate_schema import migrate_identities_and_sessions


LEGACY_TASKS_DDL = """
CREATE TABLE tasks (
    task_id VARCHAR(64) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL,
    prompt TEXT NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TEXT,
    updated_at TEXT
)
"""


def _engine(tmp_path: Path):
    return create_engine(f"sqlite:///{tmp_path / 'mig.db'}")


def _run_migration(engine, root: Path) -> None:
    with engine.begin() as conn:
        migrate_identities_and_sessions(conn, root)


def _tables(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )).fetchall()
    return {r[0] for r in rows}


def _indexes(engine) -> set[str]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'"
        )).fetchall()
    return {r[0] for r in rows}


def _task_columns(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(tasks)")).fetchall()
    return [r[1] for r in rows]


# ── Fresh database ─────────────────────────────────────────────────


def test_fresh_db_creates_all_objects(tmp_path: Path) -> None:
    """On a DB with only a legacy tasks table, migration creates everything."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)

    root = tmp_path / "home"
    _run_migration(engine, root)

    tables = _tables(engine)
    assert "identities" in tables
    assert "identity_bindings" in tables

    indexes = _indexes(engine)
    assert "idx_identities_daemon" in indexes
    assert "idx_identities_status" in indexes
    assert "idx_bindings_identity" in indexes
    assert "idx_bindings_runtime" in indexes
    assert "idx_tasks_identity" in indexes

    # AGENTS.md root directory created.
    assert (root / ".agt" / "identities").is_dir()


def test_legacy_tasks_gets_identity_did(tmp_path: Path) -> None:
    """The tasks table gains exactly one identity_did column."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)

    _run_migration(engine, tmp_path / "home")

    cols = _task_columns(engine)
    assert "identity_did" in cols
    assert cols.count("identity_did") == 1


def test_existing_task_rows_survive(tmp_path: Path) -> None:
    """Migration never disturbs existing task data."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO tasks (task_id, provider, prompt, status, created_at, updated_at) "
            "VALUES ('t-legacy', 'claude', 'hello', 'completed', '2026-01-01', '2026-01-01')"
        )

    _run_migration(engine, tmp_path / "home")

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT task_id, prompt, status, identity_did FROM tasks WHERE task_id='t-legacy'"
        )).fetchone()
    assert row == ("t-legacy", "hello", "completed", None)


# ── Idempotency ────────────────────────────────────────────────────


def test_repeated_runs_are_noop(tmp_path: Path) -> None:
    """Running the migration 3× leaves the schema identical to 1×."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)

    root = tmp_path / "home"
    _run_migration(engine, root)
    snapshot = (_tables(engine), _indexes(engine), _task_columns(engine))

    for _ in range(2):
        _run_migration(engine, root)  # must not raise

    assert (_tables(engine), _indexes(engine), _task_columns(engine)) == snapshot
    # Column was not duplicated by re-runs.
    assert _task_columns(engine).count("identity_did") == 1


def test_run_on_already_migrated_model_schema(tmp_path: Path) -> None:
    """Full modern path: create_all (model schema) → migration → no error.

    This mirrors ``create_app``'s lifespan: Base.metadata.create_all builds
    tasks WITH identity_did, then the migration's PRAGMA check skips the
    ALTER TABLE.
    """
    import asyncio
    from agt.server.models import Base

    engine = _engine(tmp_path)

    async def _create() -> None:
        # Reuse the async stack the server actually uses.
        from sqlalchemy.ext.asyncio import create_async_engine as cae
        ae = cae(f"sqlite+aiosqlite:///{tmp_path / 'mig.db'}")
        async with ae.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await ae.dispose()

    asyncio.run(_create())

    _run_migration(engine, tmp_path / "home")
    assert _task_columns(engine).count("identity_did") == 1
    # Regression: the index must also exist on the fresh path, where
    # create_all already added the column and the ALTER branch is skipped.
    assert "idx_tasks_identity" in _indexes(engine)


# ── Phase 4 Step 3: sessions PK rebuild ────────────────────────────


LEGACY_SESSIONS_DDL = """
CREATE TABLE sessions (
    task_id TEXT,
    provider TEXT,
    session_id TEXT,
    created_at TEXT,
    PRIMARY KEY (task_id, provider)
)
"""


def _sessions_pk(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sessions)")).fetchall()
    pk = sorted((r for r in rows if r[5] > 0), key=lambda r: r[5])
    return [r[1] for r in pk]


def _sessions_columns(engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(text("PRAGMA table_info(sessions)")).fetchall()
    return [r[1] for r in rows]


def test_legacy_sessions_rebuilt_with_identity_pk(tmp_path: Path) -> None:
    """A pre-Phase 4 sessions table is rebuilt to the 3-column PK, with
    existing rows preserved, task_id resolved to job_id, and identity_did=''."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO tasks (task_id, provider, prompt, status, created_at, updated_at) "
            "VALUES ('chat-1', 'claude', 'hello', 'completed', '2026-01-01', '2026-01-01')"
        )
        conn.exec_driver_sql(LEGACY_SESSIONS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO sessions (task_id, provider, session_id, created_at) "
            "VALUES ('chat-1', 'claude', 'sess-xyz', '2026-01-01')"
        )

    _run_migration(engine, tmp_path / "home")

    assert _sessions_pk(engine) == ["job_id", "identity_did", "provider"]
    cols = _sessions_columns(engine)
    assert "identity_did" in cols
    assert "updated_at" in cols

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT job_id, identity_did, provider, session_id, created_at, updated_at "
            "FROM sessions WHERE job_id='chat-1'"
        )).fetchone()
    assert row == ("chat-1", "", "claude", "sess-xyz", "2026-01-01", "2026-01-01")


def test_sessions_rebuild_is_idempotent(tmp_path: Path) -> None:
    """Running the migration again after the rebuild is a no-op."""
    engine = _engine(tmp_path)
    with engine.begin() as conn:
        conn.exec_driver_sql(LEGACY_TASKS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO tasks (task_id, provider, prompt, status, created_at, updated_at) "
            "VALUES ('chat-1', 'claude', 'hello', 'completed', '2026-01-01', '2026-01-01')"
        )
        conn.exec_driver_sql(LEGACY_SESSIONS_DDL)
        conn.exec_driver_sql(
            "INSERT INTO sessions (task_id, provider, session_id, created_at) "
            "VALUES ('chat-1', 'claude', 'sess-xyz', '2026-01-01')"
        )

    root = tmp_path / "home"
    _run_migration(engine, root)
    snapshot = _sessions_pk(engine)

    _run_migration(engine, root)  # must not raise

    assert _sessions_pk(engine) == snapshot
    with engine.connect() as conn:
        count = conn.execute(text("SELECT COUNT(*) FROM sessions")).scalar_one()
    assert count == 1
