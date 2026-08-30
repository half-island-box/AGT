"""Endpoint tests for ``GET /api/tasks/{task_id}/stream``.

Phase 5 remote streaming: verify the SSE sequence
``snapshot`` → ``delta``* → ``done``, the terminal-task shortcut, and
cursor alignment on a fresh subscriber (restart / reconnect case).

We drive the endpoint function directly (``stream_task``) and iterate its
``StreamingResponse.body_iterator`` in the same event loop as the hub, so
publishing a progress/done event from a sibling task can interleave with
the reader — this exercises the real generator logic without the ASGI
layer's buffering (``httpx.ASGITransport`` buffers a response in full,
which would deadlock an interleaved publish/read test).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import delete

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base, TaskModel
from agt.server.routes.tasks import stream_task
from agt.server.services.report_service import (
    record_task_progress_for_daemon,
    record_task_result_for_daemon,
)

SECRET = "stream-endpoint-test-secret"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


async def _seed_task(
    factory,
    task_id: str,
    daemon_id: str | None,
    *,
    status: str = "running",
    stdout: str | None = None,
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
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()


def _fresh_engine(db_path: str):
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=db_path,
        secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_stream.json",
    )
    engine = make_engine(cfg)

    async def _create_schema():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        async with engine.begin() as conn:
            await conn.run_sync(migrate_identities_and_sessions, Path("/tmp"))
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(TaskModel))
            await db.commit()

    asyncio.run(_create_schema())
    return engine


def _fake_request(factory):
    state = SimpleNamespace(session_factory=factory)
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app)


def _parse_frame(frame: str) -> dict:
    """Parse one SSE frame (``event:`` + ``data:`` lines) into a dict."""
    event_type = "message"
    data: dict | None = None
    for line in frame.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
    assert data is not None, f"no data line in frame: {frame!r}"
    data.setdefault("type", event_type)
    return data


async def _collect(resp) -> list[dict]:
    events: list[dict] = []
    async for chunk in resp.body_iterator:
        events.append(_parse_frame(chunk))
    return events


def test_stream_snapshot_delta_done_sequence() -> None:
    engine = _fresh_engine("/tmp/_stream_sequence.db")
    factory = make_session_factory(engine)

    async def _run():
        await _seed_task(factory, "t-seq", "d1", stdout="")
        resp = await stream_task("t-seq", _fake_request(factory))
        reader = asyncio.create_task(_collect(resp))
        await asyncio.sleep(0.05)

        async with session_scope(factory) as db:
            await record_task_progress_for_daemon(
                db, daemon_id="d1", task_id="t-seq", stdout_prefix="hello",
            )
        await asyncio.sleep(0.05)

        async with session_scope(factory) as db:
            await record_task_result_for_daemon(
                db,
                daemon_id="d1",
                task_id="t-seq",
                exit_code=0,
                stdout="hello world",
                stderr="",
                duration=1.2,
                status="completed",
                session_id="sess-1",
                resume_rejected=False,
            )

        events = await asyncio.wait_for(reader, timeout=2)

        assert len(events) == 3
        assert events[0]["type"] == "snapshot"
        assert events[0]["status"] == "running"
        assert events[0]["stdout"] == ""
        assert events[1]["type"] == "delta"
        assert events[1]["chunk"] == "hello"
        assert events[2]["type"] == "done"
        assert events[2]["status"] == "completed"
        assert events[2]["stdout"] == "hello world"
        assert events[2]["session_id"] == "sess-1"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_stream_terminal_task_snapshot_then_done() -> None:
    """A task already terminal at subscribe time yields snapshot + done
    immediately (no ping interval)."""
    engine = _fresh_engine("/tmp/_stream_terminal.db")
    factory = make_session_factory(engine)

    async def _run():
        await _seed_task(
            factory, "t-term", "d1", status="completed", stdout="already done",
        )
        resp = await stream_task("t-term", _fake_request(factory))
        events = await _collect(resp)

        assert len(events) == 2
        assert events[0]["type"] == "snapshot"
        assert events[0]["stdout"] == "already done"
        assert events[1]["type"] == "done"
        assert events[1]["status"] == "completed"
        assert events[1]["stdout"] == "already done"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_stream_reconnect_aligns_cursor_to_snapshot() -> None:
    """A fresh subscriber's cursor is set to ``len(snapshot)`` so a later
    progress event emits only the *new* growth — never the prefix the
    client has already seen (server-restart alignment)."""
    engine = _fresh_engine("/tmp/_stream_align.db")
    factory = make_session_factory(engine)

    async def _run():
        await _seed_task(factory, "t-align", "d1", stdout="hello")
        resp = await stream_task("t-align", _fake_request(factory))
        reader = asyncio.create_task(_collect(resp))
        await asyncio.sleep(0.05)

        async with session_scope(factory) as db:
            await record_task_progress_for_daemon(
                db, daemon_id="d1", task_id="t-align", stdout_prefix="hello!",
            )
        await asyncio.sleep(0.05)

        async with session_scope(factory) as db:
            await record_task_result_for_daemon(
                db,
                daemon_id="d1",
                task_id="t-align",
                exit_code=0,
                stdout="hello!",
                stderr="",
                duration=0.5,
                status="completed",
                session_id=None,
                resume_rejected=False,
            )

        events = await asyncio.wait_for(reader, timeout=2)

        assert len(events) == 3
        assert events[0]["type"] == "snapshot"
        assert events[0]["stdout"] == "hello"
        assert events[1]["type"] == "delta"
        assert events[1]["chunk"] == "!"
        assert events[2]["type"] == "done"
        assert events[2]["stdout"] == "hello!"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_stream_missing_task_returns_404() -> None:
    engine = _fresh_engine("/tmp/_stream_404.db")
    factory = make_session_factory(engine)

    async def _run():
        try:
            await stream_task("nope", _fake_request(factory))
            assert False, "should have raised"
        except HTTPException as exc:
            assert exc.status_code == 404

    asyncio.run(_run())
    asyncio.run(engine.dispose())
