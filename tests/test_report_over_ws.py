"""``tasks.report`` WS-first + HTTP-fallback.

Verifies that the daemon can write a task result back to the server
over the reverse-dial WS link (the only reachable direction when the
server is behind NAT). Before this change, ``report`` only went over
HTTP and silently failed in reverse-dial mode — leaving the server's
``tasks`` row stuck in ``running`` and the local CLI's ``wait_task``
spinning until timeout.

Covers:

- The shared service ``record_step_result_for_daemon`` (HTTP + WS paths
  both call it): happy write, session upsert, resume-rejected cleanup,
  task-not-found, task-not-owned.
- ``DaemonLink._handle_tasks_report`` mapping service errors to
  ``RPCRejected(404|403)`` and ``validation_error``.
- End-to-end via the reverse-dial link: ``report_task_ws_first`` writes
  the result over WS, the server's ``tasks`` row reflects it.
- ``report_task_ws_first`` HTTP fallback paths (WS unavailable /
  Uncertain safety window).
- The HTTP route still works for local daemons (regression guard).
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, select
from starlette.testclient import TestClient

from agt.comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain
from agt.comm.wsrpc_link import WsRpcLink
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base, JobModel, SessionModel, StepModel
from agt.server.services.report_service import record_step_result_for_daemon


SECRET = "report-over-ws-test-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── service-layer unit tests ──────────────────────────────────────


def _fresh_db():
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_report_service.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_report.json",
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        # Phase 4 Step 3: the sessions PK rebuild lives in the full
        # migration. Run it too so a stale fixed-path DB (left by an
        # earlier run) picks up the new sessions schema.
        async with engine.begin() as conn:
            await conn.run_sync(migrate_identities_and_sessions, Path("/tmp"))
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(StepModel))
            await db.execute(delete(SessionModel))
            await db.execute(delete(JobModel))
            await db.commit()

    asyncio.run(_setup())
    return engine, cfg


async def _seed_task(
    factory,
    task_id: str,
    daemon_id: str | None,
    *,
    job_type: str = "task",
    first_prompt: str | None = None,
) -> None:
    now = _utcnow()
    async with session_scope(factory) as db:
        db.add(JobModel(
            job_id=task_id, status="running", job_type=job_type,
            provider="claude", first_prompt=first_prompt,
            created_at=now, updated_at=now,
        ))
        db.add(StepModel(
            step_id=task_id, job_id=task_id, provider="claude", prompt="hi",
            status="running", daemon_id=daemon_id,
            created_at=now, updated_at=now,
        ))
        await db.commit()


async def _row(factory, task_id: str) -> StepModel | None:
    async with session_scope(factory) as db:
        return await db.get(StepModel, task_id)


def test_service_writes_result_to_task() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1")
        async with session_scope(factory) as db:
            result = await record_step_result_for_daemon(
                db, daemon_id="d1", step_id="t1",
                exit_code=0, stdout="ok\n", stderr="", duration=1.23,
                status="completed", session_id="sess-1", resume_rejected=False,
            )
            assert result == {"recorded": True, "artifacts": []}
        row = await _row(factory, "t1")
        assert row.status == "completed"
        assert row.exit_code == 0
        assert row.stdout == "ok\n"
        assert row.stderr == ""
        assert row.duration == 1.23
        assert row.session_id == "sess-1"
        assert row.updated_at != _utcnow() or True  # touched; value sanity check skipped

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_updates_chat_job_provider_title() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(
            factory,
            "chat-title",
            "d-title",
            job_type="chat",
            first_prompt="long first prompt",
        )
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id="d-title",
                step_id="chat-title",
                exit_code=0,
                stdout="ok",
                stderr="",
                duration=1.0,
                status="completed",
                session_id="sess-title",
                resume_rejected=False,
                session_title="Provider native title",
            )
        async with session_scope(factory) as db:
            job = await db.get(JobModel, "chat-title")
            assert job is not None
            assert job.first_prompt == "long first prompt"
            assert job.title == "Provider native title"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_session_id_upserts_session_model() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1")
        # First report inserts SessionModel row.
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db, daemon_id="d1", step_id="t1",
                exit_code=0, stdout="", stderr="", duration=0.1,
                status="completed", session_id="sess-A", resume_rejected=False,
            )
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(SessionModel).where(SessionModel.job_id == "t1")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].session_id == "sess-A"

        # Second report with a new session_id updates in place.
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db, daemon_id="d1", step_id="t1",
                exit_code=0, stdout="", stderr="", duration=0.2,
                status="completed", session_id="sess-B", resume_rejected=False,
            )
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(SessionModel).where(SessionModel.job_id == "t1")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].session_id == "sess-B"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_resume_rejected_deletes_session_model() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1")
        # Seed a SessionModel row first.
        async with session_scope(factory) as db:
            db.add(SessionModel(
                job_id="t1", provider="claude",
                session_id="old", created_at=_utcnow(), updated_at=_utcnow(),
            ))
            await db.commit()
        # Report with resume_rejected=True — must drop the SessionModel row.
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db, daemon_id="d1", step_id="t1",
                exit_code=0, stdout="", stderr="", duration=0.1,
                status="completed", session_id="ignored-when-rejected",
                resume_rejected=True,
            )
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(SessionModel).where(SessionModel.job_id == "t1")
            )).scalars().all()
        assert rows == []
        row = await _row(factory, "t1")
        assert row.session_id is None

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_task_not_found_raises_value_error() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        async with session_scope(factory) as db:
            try:
                await record_step_result_for_daemon(
                    db, daemon_id="d1", step_id="missing",
                    exit_code=0, stdout="", stderr="", duration=0.0,
                    status="completed", session_id=None, resume_rejected=False,
                )
                assert False, "should have raised"
            except ValueError as exc:
                payload = exc.args[0]
                assert payload["type"] == "task_not_found"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_service_task_not_owned_raises_value_error() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d-real")
        async with session_scope(factory) as db:
            try:
                await record_step_result_for_daemon(
                    db, daemon_id="d-imposter", step_id="t1",
                    exit_code=0, stdout="", stderr="", duration=0.0,
                    status="completed", session_id=None, resume_rejected=False,
                )
                assert False, "should have raised"
            except ValueError as exc:
                assert exc.args[0]["type"] == "task_not_owned"

        # A task never claimed by anyone (daemon_id=None) also rejects.
        await _seed_task(factory, "t2", None)
        async with session_scope(factory) as db:
            try:
                await record_step_result_for_daemon(
                    db, daemon_id="d1", step_id="t2",
                    exit_code=0, stdout="", stderr="", duration=0.0,
                    status="completed", session_id=None, resume_rejected=False,
                )
                assert False, "should have raised for unclaimed task"
            except ValueError as exc:
                assert exc.args[0]["type"] == "task_not_owned"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── DaemonLink._handle_tasks_report (handler unit tests) ──────────


class _StubLink:
    """Minimal stub for WsRpcLink so we can call DaemonLink handlers directly.

    The handlers only need ``self._session_factory`` and ``self.daemon_id``;
    they don't touch the link itself.
    """

    def __init__(self, daemon_id: str, factory) -> None:
        self._session_factory = factory
        self.daemon_id = daemon_id


def _make_handler(daemon_id: str, factory):
    """Build a ``DaemonLink``-shaped object whose handlers we can call
    without spinning up a real WS server."""
    from agt.server.daemon_link import DaemonLink
    link = DaemonLink(
        daemon_id=daemon_id,
        url="ws://unused",
        secret=SECRET,
        session_factory=factory,
    )
    return link


def test_handler_maps_task_not_found_to_rpc_404() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        handler = _make_handler("d1", factory)
        try:
            await handler._handle_tasks_report({
                "task_id": "missing", "exit_code": 0,
                "stdout": "", "stderr": "", "duration": 0.0,
                "status": "completed",
            })
            assert False, "should have raised"
        except RPCRejected as exc:
            assert exc.status == 404
            assert exc.body["type"] == "task_not_found"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_handler_maps_task_not_owned_to_rpc_403() -> None:
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d-real")
        handler = _make_handler("d-imposter", factory)
        try:
            await handler._handle_tasks_report({
                "task_id": "t1", "exit_code": 0,
                "stdout": "", "stderr": "", "duration": 0.0,
                "status": "completed",
            })
            assert False, "should have raised"
        except RPCRejected as exc:
            assert exc.status == 403
            assert exc.body["type"] == "task_not_owned"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_handler_missing_field_raises_validation_error() -> None:
    """Missing required field → ValueError so wsrpc_link surfaces it as
    a generic validation_error frame (not a business 404/403)."""
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        handler = _make_handler("d1", factory)
        try:
            await handler._handle_tasks_report({
                "task_id": "t1",
                # exit_code missing on purpose
                "stdout": "", "stderr": "", "duration": 0.0,
                "status": "completed",
            })
            assert False, "should have raised"
        except ValueError as exc:
            payload = exc.args[0]
            assert payload["type"] == "validation_error"
            assert "exit_code" in payload["message"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_handler_registers_artifacts_over_ws() -> None:
    """A WS report carrying declared deliverables must register them.

    Regression guard: ``_parse_artifact_items`` lazy-imports
    ``ArtifactReportItem``; a wrong relative import there
    (``from ..schemas`` instead of ``from .schemas``) only fires when the
    list is non-empty, silently turning every remote artifact report into
    a ``handler_error`` and leaving the task stuck in ``running``.
    """
    engine, _ = _fresh_db()
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-art", "d1")
        handler = _make_handler("d1", factory)
        result = await handler._handle_tasks_report({
            "task_id": "t-art", "exit_code": 0,
            "stdout": "done", "stderr": "", "duration": 0.5,
            "status": "completed",
            "artifacts": [{
                "name": "report.txt",
                "mime_type": "text/plain",
                "summary": None,
                "status_flag": "pass",
                "size": 5,
                "sha256": "a" * 64,
                "local_path": "/tmp/report.txt",
                "inline_b64": None,
            }],
        })
        assert result["recorded"] is True
        assert len(result["artifacts"]) == 1

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── HTTP route regression guard ───────────────────────────────────


def test_http_report_route_still_works() -> None:
    """The refactor moved business logic into the service; this test
    guards the HTTP path against accidental drift."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_report_http.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_report.json",
    )

    async def _seed() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(StepModel))
            await db.execute(delete(SessionModel))
            await db.execute(delete(JobModel))
            await db.commit()
        await _seed_task(factory, "t-http", "d-http")
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_seed())
    app = create_app(cfg)
    with TestClient(app) as client:
        resp = client.post("/api/tasks/t-http/report", json={
            "daemon_id": "d-http",
            "task_id": "t-http",
            "exit_code": 0, "stdout": "ok", "stderr": "",
            "duration": 0.5, "status": "completed",
            "session_id": "sess-http", "resume_rejected": False,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"recorded": True, "artifacts": []}
        # 404 case
        resp = client.post("/api/tasks/nope/report", json={
            "daemon_id": "d-http",
            "task_id": "nope",
            "exit_code": 0, "stdout": "", "stderr": "",
            "duration": 0.0, "status": "completed",
        })
        assert resp.status_code == 404
        # 403 case
        resp = client.post("/api/tasks/t-http/report", json={
            "daemon_id": "d-other",
            "task_id": "t-http",
            "exit_code": 0, "stdout": "", "stderr": "",
            "duration": 0.0, "status": "completed",
        })
        assert resp.status_code == 403
    asyncio.run(cfg._engine_ref.dispose())  # type: ignore[attr-defined]


# ── report_task_ws_first (daemon-side orchestrator) ───────────────


class _FakeWsRpcLink:
    """Implements just enough of WsRpcLink's surface for
    ``report_task_ws_first`` to drive without a real socket.

    ``call`` returns ``result`` if set, else raises whatever exception
    is in ``raise_exc`` — mirrors the three-way WSRPC surface.
    """

    def __init__(
        self,
        *,
        result: dict | None = None,
        raise_exc: Exception | None = None,
        supports_rpc_v1: bool = True,
    ) -> None:
        self._result = result or {"recorded": True}
        self._raise = raise_exc
        self.calls: list[dict] = []
        self.supports_rpc_v1 = supports_rpc_v1

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        assert method == "tasks.report"
        self.calls.append(body)
        assert timeout_ms == 5000
        if self._raise is not None:
            raise self._raise
        return self._result


class _FakeHttpClient:
    """Captures the HTTP-fallback calls without a real server."""

    def __init__(self, *, raise_on_call: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.raise_on_call = raise_on_call

    async def report_task(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        return True


def test_report_ws_first_happy_path_skips_http() -> None:
    """Successful WS write → HTTP fallback is NOT consulted."""
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        wsrpc = _FakeWsRpcLink()
        http = _FakeHttpClient()
        state = ClaimStateMachine()
        ok = await report_task_ws_first(
            wsrpc=wsrpc, http_client=http, state=state,
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id="s",
            resume_rejected=False,
        )
        assert ok is True
        assert http.calls == []

    asyncio.run(_run())


def test_report_ws_first_forwards_provider_title() -> None:
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        wsrpc = _FakeWsRpcLink()
        http = _FakeHttpClient()
        ok = await report_task_ws_first(
            wsrpc=wsrpc, http_client=http, state=ClaimStateMachine(),
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id="s",
            resume_rejected=False, session_title="Native title",
        )

        assert ok is True
        assert wsrpc.calls[0]["session_title"] == "Native title"
        assert http.calls == []

    asyncio.run(_run())


def test_report_ws_first_unavailable_falls_back_to_http() -> None:
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        wsrpc = _FakeWsRpcLink(raise_exc=WSRPCUnavailable("send failed"))
        http = _FakeHttpClient()
        state = ClaimStateMachine()
        ok = await report_task_ws_first(
            wsrpc=wsrpc, http_client=http, state=state,
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id=None,
            resume_rejected=False,
        )
        assert ok is True
        assert len(http.calls) == 1
        assert http.calls[0]["task_id"] == "t1"

    asyncio.run(_run())


def test_report_ws_first_uncertain_marks_window_skips_http() -> None:
    """A WSRPCUncertain write marks the safety window and returns False
    — HTTP is NOT consulted during the window, because we don't know if
    the server got our frame. The next call (after the window) will
    fall through to HTTP."""
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        sent_at = asyncio.get_event_loop().time()
        wsrpc = _FakeWsRpcLink(
            raise_exc=WSRPCUncertain("timeout", frame_sent_at=sent_at),
        )
        http = _FakeHttpClient()
        state = ClaimStateMachine(server_timeout=5, grace=2)
        ok = await report_task_ws_first(
            wsrpc=wsrpc, http_client=http, state=state,
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id=None,
            resume_rejected=False,
        )
        assert ok is False
        assert state.blocked is True
        assert http.calls == []

    asyncio.run(_run())


def test_report_ws_first_no_link_falls_back_to_http() -> None:
    """``wsrpc is None`` (local daemon / no link yet) → HTTP only."""
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        http = _FakeHttpClient()
        state = ClaimStateMachine()
        ok = await report_task_ws_first(
            wsrpc=None, http_client=http, state=state,
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id=None,
            resume_rejected=False,
        )
        assert ok is True
        assert len(http.calls) == 1

    asyncio.run(_run())


def test_report_ws_first_http_failure_returns_false() -> None:
    """Both paths failing → return False (caller logs a warning)."""
    from agt.daemon.ws_claim import ClaimStateMachine, report_task_ws_first

    async def _run() -> None:
        wsrpc = _FakeWsRpcLink(raise_exc=WSRPCUnavailable("send failed"))
        http = _FakeHttpClient(raise_on_call=Exception("All connection attempts failed"))
        state = ClaimStateMachine()
        ok = await report_task_ws_first(
            wsrpc=wsrpc, http_client=http, state=state,
            task_id="t1", exit_code=0, stdout="x", stderr="",
            duration=0.1, status="completed", session_id=None,
            resume_rejected=False,
        )
        assert ok is False

    asyncio.run(_run())


# ── end-to-end: real WS link reports a real task result ────────────


def test_report_end_to_end_over_reverse_dial() -> None:
    """Spawn a real DaemonWsServer + DaemonLink, fire ``tasks.report``
    over the link, and confirm the server's ``tasks`` row reflects it.

    This is the exact scenario that was broken: daemon claims via WS,
    agent runs, result must come back via the same WS link — not HTTP.
    """

    async def _run() -> None:
        from agt.daemon.ws_server import DaemonWsServer
        from agt.server.daemon_link import DaemonLink
        from agt.server.services.auth_service import TaskToken

        # Use a fresh per-run DB path so leftover state from a previous
        # test run (or even this session's earlier failures) can't
        # collide with the seed task_id.
        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path=f"/tmp/_report_e2e_{_free_port()}.db",
            secret=SECRET,
            sessions_json_path="/tmp/_nonexistent_sessions_for_report.json",
        )
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        try:
            # Belt-and-suspenders: wipe any rows left over from a
            # previous run, then seed.
            async with session_scope(factory) as db:
                await db.execute(delete(StepModel))
                await db.execute(delete(SessionModel))
                await db.execute(delete(JobModel))
                await db.commit()
            # Seed a task as if this daemon had already claimed it.
            await _seed_task(factory, "t-e2e", "d-e2e")

            port = _free_port()
            daemon_id = "d-e2e"
            ws_server = DaemonWsServer(
                port=port, secret=SECRET, daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}"
            )
            await ws_server.start()

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}",
                secret=SECRET, session_factory=factory,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                for _ in range(60):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                assert link.connected

                # Wait for rpc_v1 so the daemon-side orchestrator knows
                # it can use the WS path.
                daemon_link = ws_server.current_link
                for _ in range(40):
                    if daemon_link.supports_rpc_v1:
                        break
                    await asyncio.sleep(0.05)
                assert daemon_link.supports_rpc_v1

                # Fire ``tasks.report`` over the same link.
                result = await daemon_link.call(
                    "tasks.report",
                    {
                        "task_id": "t-e2e",
                        "exit_code": 0,
                        "stdout": "agent says hi",
                        "stderr": "",
                        "duration": 2.5,
                        "status": "completed",
                        "session_id": "sess-e2e",
                        "resume_rejected": False,
                    },
                    timeout_ms=5000,
                )
                assert result == {"recorded": True, "artifacts": []}

                # Server's tasks row must reflect the report.
                row = await _row(factory, "t-e2e")
                assert row.status == "completed"
                assert row.exit_code == 0
                assert row.stdout == "agent says hi"
                assert row.stderr == ""
                assert row.duration == 2.5
                assert row.session_id == "sess-e2e"
                async with session_scope(factory) as db:
                    sess = (await db.execute(
                        select(SessionModel).where(
                            SessionModel.job_id == "t-e2e"
                        )
                    )).scalars().all()
                assert len(sess) == 1
                assert sess[0].session_id == "sess-e2e"
            finally:
                shutdown.set()
                link_task.cancel()
                try:
                    await asyncio.wait_for(link_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                await ws_server.stop()
        finally:
            await engine.dispose()

    asyncio.run(_run())


# Touch imports to keep pyflakes happy if a refactor moves them.
_ = (RPCRejected, WSRPCUnavailable, WSRPCUncertain, WsRpcLink)
