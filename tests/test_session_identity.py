"""Phase 4 Step 3 — session identity dimension tests.

The sessions table PK becomes ``(task_id, identity_did, provider)`` so the
same ``task_id`` under different identities (or no identity at all) holds
independent session pointers. These tests cover the write path
(``record_step_result_for_daemon``) and the read path (``GET /api/sessions``):

- reporting stamps the task's ``identity_did`` into the session row
  (``""`` for identity-less tasks);
- ``resume_rejected`` deletes only the matching identity slot;
- the route does an identity-aware exact lookup and a task-wide list.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import select
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, JobModel, SessionModel, StepModel
from agt.server.services.report_service import record_step_result_for_daemon


SECRET = "session-identity-test-secret"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _engine_and_factory(tmp_path):
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "sessions.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    return engine, make_session_factory(engine), cfg


async def _seed_task(
    factory, task_id: str, daemon_id: str, identity_did: str | None = None,
) -> None:
    now = _utcnow()
    async with session_scope(factory) as db:
        db.add(JobModel(
            job_id=task_id, status="running", job_type="task",
            provider="claude", identity_did=identity_did,
            created_at=now, updated_at=now,
        ))
        db.add(StepModel(
            step_id=task_id, job_id=task_id, provider="claude", prompt="hi",
            status="running", daemon_id=daemon_id,
            identity_did=identity_did,
            created_at=now, updated_at=now,
        ))
        await db.commit()


async def _session_rows(factory, task_id: str) -> list[SessionModel]:
    async with session_scope(factory) as db:
        return (await db.execute(
            select(SessionModel).where(SessionModel.job_id == task_id)
        )).scalars().all()


async def _report(factory, task_id: str, daemon_id: str, *, session_id, resume_rejected=False) -> None:
    async with session_scope(factory) as db:
        await record_step_result_for_daemon(
            db, daemon_id=daemon_id, step_id=task_id,
            exit_code=0, stdout="", stderr="", duration=0.1,
            status="completed", session_id=session_id,
            resume_rejected=resume_rejected,
        )


# ── Service: write path ────────────────────────────────────────────


def test_report_stamps_identity_did_into_session(tmp_path) -> None:
    engine, factory, _ = _engine_and_factory(tmp_path)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1", identity_did="did:agt:cr")
        await _report(factory, "t1", "d1", session_id="sess-A")
        rows = await _session_rows(factory, "t1")
        assert len(rows) == 1
        assert rows[0].identity_did == "did:agt:cr"
        assert rows[0].session_id == "sess-A"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_report_without_identity_uses_empty_sentinel(tmp_path) -> None:
    engine, factory, _ = _engine_and_factory(tmp_path)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1", identity_did=None)
        await _report(factory, "t1", "d1", session_id="sess-A")
        rows = await _session_rows(factory, "t1")
        assert len(rows) == 1
        assert rows[0].identity_did == ""

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_resume_rejected_deletes_only_matching_identity(tmp_path) -> None:
    engine, factory, _ = _engine_and_factory(tmp_path)

    async def _run() -> None:
        await _seed_task(factory, "t1", "d1", identity_did="did:agt:cr")
        now = _utcnow()
        # Seed two session rows for the same task_id+provider, different
        # identities. Only the cr slot should be dropped on reject.
        async with session_scope(factory) as db:
            db.add(SessionModel(
                job_id="t1", identity_did="did:agt:cr", provider="claude",
                session_id="cr-sess", created_at=now, updated_at=now,
            ))
            db.add(SessionModel(
                job_id="t1", identity_did="did:agt:translator", provider="claude",
                session_id="tr-sess", created_at=now, updated_at=now,
            ))
            await db.commit()

        await _report(factory, "t1", "d1", session_id="ignored", resume_rejected=True)

        rows = await _session_rows(factory, "t1")
        assert {r.identity_did for r in rows} == {"did:agt:translator"}

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── Route: read path ───────────────────────────────────────────────


def test_session_route_exact_and_list(tmp_path) -> None:
    engine, factory, cfg = _engine_and_factory(tmp_path)
    now = _utcnow()

    async def _seed() -> None:
        async with session_scope(factory) as db:
            db.add(SessionModel(
                job_id="chat-1", identity_did="did:agt:cr", provider="claude",
                session_id="cr-sess", created_at=now, updated_at=now,
            ))
            db.add(SessionModel(
                job_id="chat-1", identity_did="", provider="claude",
                session_id="legacy-sess", created_at=now, updated_at=now,
            ))
            await db.commit()

    asyncio.run(_seed())

    app = create_app(cfg)
    client = TestClient(app)
    try:
        # Exact lookup with identity → the cr slot.
        resp = client.get(
            "/api/sessions", params={
                "job_id": "chat-1", "provider": "claude",
                "identity_did": "did:agt:cr",
            }
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "cr-sess"

        # Exact lookup with no identity → the legacy "" slot.
        resp = client.get(
            "/api/sessions", params={"job_id": "chat-1", "provider": "claude"}
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "legacy-sess"

        # Unknown provider → 404.
        resp = client.get(
            "/api/sessions", params={"job_id": "chat-1", "provider": "codex"}
        )
        assert resp.status_code == 404

        # Job-wide list → both slots.
        resp = client.get("/api/sessions", params={"job_id": "chat-1"})
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert {(s["identity_did"], s["provider"]) for s in sessions} == {
            ("did:agt:cr", "claude"), ("", "claude"),
        }
    finally:
        client.close()
        asyncio.run(engine.dispose())
