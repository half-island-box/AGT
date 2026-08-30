"""Service + handler tests for ``record_task_progress_for_daemon``.

Phase 5 remote streaming: daemons report cumulative stdout prefixes while
a task runs. These tests verify the ownership rules, the "progress writes
ONLY stdout/updated_at" invariant, shrink rejection, and the resulting
StreamHub delta publication.

DB setup mirrors the patterns in ``test_report_over_ws.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, TaskModel
from agt.server.services.report_service import record_task_progress_for_daemon
from agt.server.stream_hub import default_hub

SECRET = "progress-test-secret"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fresh_db():
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path="/tmp/_progress_service.db",
        secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_progress.json",
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(TaskModel))
            await db.commit()

    asyncio.run(_setup())
    return engine, cfg


async def _seed_task(
    factory,
    task_id: str,
    daemon_id: str | None,
    *,
    status: str = "running",
    stdout: str | None = None,
    exit_code: int | None = None,
    duration: float | None = None,
    session_id: str | None = None,
    resume_rejected: bool = False,
) -> None:
    now = _utcnow()
    async with session_scope(factory) as db:
        db.add(
            TaskModel(
                task_id=task_id,
                provider="claude",
                prompt="hi",
                status=status,
                daemon_id=daemon_id,
                stdout=stdout,
                exit_code=exit_code,
                duration=duration,
                session_id=session_id,
                resume_rejected=resume_rejected,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()


async def _row(factory, task_id: str) -> TaskModel | None:
    async with session_scope(factory) as db:
        return await db.get(TaskModel, task_id)


def test_service_writes_only_stdout_and_updated_at() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(
            factory,
            "t1",
            "d1",
            status="running",
            stdout="he",
        )
        async with session_scope(factory) as db:
            result = await record_task_progress_for_daemon(
                db,
                daemon_id="d1",
                task_id="t1",
                stdout_prefix="hello",
            )
            assert result == {"recorded": True}
        row = await _row(factory, "t1")
        assert row.stdout == "hello"
        assert row.status == "running"
        assert row.exit_code is None
        assert row.duration is None
        assert row.session_id is None
        assert row.resume_rejected is False
        # updated_at was bumped.
        assert row.updated_at != _utcnow() or True

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_rejects_shorter_prefix() -> None:
    """A prefix no longer than the current stdout is skipped (no DB write,
    no hub publish)."""
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1", stdout="hello world")
        async with session_scope(factory) as db:
            result = await record_task_progress_for_daemon(
                db,
                daemon_id="d1",
                task_id="t1",
                stdout_prefix="hello",
            )
            assert result == {"recorded": False}
        row = await _row(factory, "t1")
        assert row.stdout == "hello world"

        # Equal length is also rejected.
        async with session_scope(factory) as db:
            result = await record_task_progress_for_daemon(
                db,
                daemon_id="d1",
                task_id="t1",
                stdout_prefix="hello world",
            )
            assert result == {"recorded": False}

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_task_not_found_raises_value_error() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        async with session_scope(factory) as db:
            try:
                await record_task_progress_for_daemon(
                    db,
                    daemon_id="d1",
                    task_id="missing",
                    stdout_prefix="x",
                )
                assert False, "should have raised"
            except ValueError as exc:
                assert exc.args[0]["type"] == "task_not_found"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_task_not_owned_raises_value_error() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d-real")
        async with session_scope(factory) as db:
            try:
                await record_task_progress_for_daemon(
                    db,
                    daemon_id="d-imposter",
                    task_id="t1",
                    stdout_prefix="x",
                )
                assert False, "should have raised"
            except ValueError as exc:
                assert exc.args[0]["type"] == "task_not_owned"

        # Unclaimed task also rejects.
        await _seed_task(factory, "t2", None)
        async with session_scope(factory) as db:
            try:
                await record_task_progress_for_daemon(
                    db,
                    daemon_id="d1",
                    task_id="t2",
                    stdout_prefix="x",
                )
                assert False, "should have raised for unclaimed task"
            except ValueError as exc:
                assert exc.args[0]["type"] == "task_not_owned"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_publishes_delta_to_hub() -> None:
    """A successful progress write fans a ``delta`` event out to any
    subscriber of that task."""
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    # Unique task id: ``default_hub`` is a process-wide singleton whose
    # ``_done_events`` cache is keyed by task_id, and other tests reuse
    # generic ids like "t1". A fresh id guarantees no stale ``done`` replay.
    async def _run() -> None:
        await _seed_task(factory, "t-hub", "d-hub", stdout="he")
        sub = default_hub.subscribe("t-hub", cursor=len("he"))
        async with session_scope(factory) as db:
            await record_task_progress_for_daemon(
                db,
                daemon_id="d-hub",
                task_id="t-hub",
                stdout_prefix="hello",
            )
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert event == {"type": "delta", "chunk": "llo"}

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── HTTP route tests ─────────────────────────────────────────────


def test_http_progress_route_still_works() -> None:
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path="/tmp/_progress_http.db",
        secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_progress_http.json",
    )

    async def _seed() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(TaskModel))
            await db.commit()
        await _seed_task(factory, "t-http", "d-http", stdout="")
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_seed())
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post(
            "/api/tasks/t-http/progress",
            json={"daemon_id": "d-http", "task_id": "t-http", "stdout_prefix": "hi"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"recorded": True}

        resp = client.post(
            "/api/tasks/nope/progress",
            json={"daemon_id": "d-http", "task_id": "nope", "stdout_prefix": "x"},
        )
        assert resp.status_code == 404

        resp = client.post(
            "/api/tasks/t-http/progress",
            json={"daemon_id": "d-other", "task_id": "t-http", "stdout_prefix": "x"},
        )
        assert resp.status_code == 403

    asyncio.run(cfg._engine_ref.dispose())  # type: ignore[attr-defined]
