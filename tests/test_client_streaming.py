"""Client-side SSE parsing tests for ``ServerClient.stream_task``.

These run against a real uvicorn server over the loopback socket so the
full wire path is exercised — ``httpx.AsyncClient.send(..., stream=True)``
builds the request, uvicorn streams ``StreamingResponse``, and
``stream_task`` parses the SSE frames back into event dicts.

This exists specifically to guard the request/stream plumbing (a bug here
is invisible to the endpoint-function tests, which bypass ``ServerClient``
entirely, and to the CLI stubs, which replace ``stream_task``).
"""

from __future__ import annotations

import asyncio
import socket
import threading
from datetime import datetime, timezone

import pytest
import uvicorn
from sqlalchemy import delete

from agt.comm.client import ServerClient
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, StepModel
from agt.server.services.report_service import (
    record_step_progress_for_daemon,
    record_step_result_for_daemon,
)

SECRET = "client-stream-test-secret"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class _Server:
    """A real uvicorn server on a loopback port, torn down after the test."""

    def __init__(self, db_path: str, factory) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.factory = factory
        cfg = ServerConfig(
            host="127.0.0.1",
            port=self.port,
            db_path=db_path,
            secret=SECRET,
            sessions_json_path=f"{db_path}.sessions.json",
        )
        app = create_app(cfg)
        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)

    def start(self) -> None:
        self._thread.start()
        for _ in range(200):
            if self._server.started:
                return
            import time

            time.sleep(0.05)
        raise RuntimeError("uvicorn server did not start")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=5)


async def _seed_task(factory, task_id: str, daemon_id: str | None) -> None:
    now = _utcnow()
    async with session_scope(factory) as db:
        db.add(
            StepModel(
                step_id=task_id,
                provider="claude",
                prompt="hi",
                status="running",
                daemon_id=daemon_id,
                stdout="",
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()


def test_stream_task_parses_snapshot_delta_done(tmp_path) -> None:
    db_path = str(tmp_path / "client-stream.db")
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=db_path,
        secret=SECRET,
        sessions_json_path=str(tmp_path / "sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        async with session_scope(factory) as db:
            await db.execute(delete(StepModel))
            await db.commit()

    asyncio.run(_setup())
    server = _Server(db_path, factory)
    server.start()

    async def _run() -> None:
        await _seed_task(factory, "t-client", "d-client")
        async with ServerClient(
            base_url=server.base_url, daemon_id="d-client"
        ) as client:
            gen = client.stream_task("t-client")

            # First event: snapshot (drives the generator to subscribe).
            snap = await asyncio.wait_for(gen.__anext__(), timeout=5)
            assert snap["type"] == "snapshot"
            assert snap["stdout"] == ""

            # Let the server's subscribe settle, then publish progress.
            await asyncio.sleep(0.1)
            async with session_scope(factory) as db:
                await record_step_progress_for_daemon(
                    db,
                    daemon_id="d-client",
                    step_id="t-client",
                    stdout_prefix="hello",
                )
            delta = await asyncio.wait_for(gen.__anext__(), timeout=5)
            assert delta == {"type": "delta", "chunk": "hello"}

            async with session_scope(factory) as db:
                await record_step_result_for_daemon(
                    db,
                    daemon_id="d-client",
                    step_id="t-client",
                    exit_code=0,
                    stdout="hello world",
                    stderr="",
                    duration=1.2,
                    status="completed",
                    session_id="sess-client",
                    resume_rejected=False,
                )
            done = await asyncio.wait_for(gen.__anext__(), timeout=5)
            assert done["type"] == "done"
            assert done["stdout"] == "hello world"
            assert done["session_id"] == "sess-client"

    try:
        asyncio.run(_run())
    finally:
        server.stop()
        asyncio.run(engine.dispose())
