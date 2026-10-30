"""Unified run-history by cwd (Phase 6).

Covers:
- JobModel.job_type is "chat"/"sop"/"goal" for the three first-class modes.
- JobModel.cwd is written from work_dir/agent_cwd/cwd on submission.
- GET /api/runs merges the three modes and filters by cwd/daemon_id.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import yaml
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "unified-runs-test-secret"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _build_app(tmp_path: Path):
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return client, factory, _dispose


def _register_claude(client: TestClient, daemon_id: str = "local") -> None:
    resp = client.post(
        "/api/runtimes/register",
        json={
            "daemon_id": daemon_id,
            "runtimes": [
                {
                    "name": "claude",
                    "type": "claude",
                    "version": "1.0",
                    "status": "online",
                    "device_name": "test",
                }
            ],
        },
    )
    assert resp.status_code in (200, 201), resp.text


def test_runs_filtered_by_cwd(tmp_path: Path) -> None:
    client, _factory, dispose = _build_app(tmp_path)
    try:
        _register_claude(client)
        cwd = str(tmp_path / "project-x")
        other_cwd = str(tmp_path / "project-y")

        # Chat
        chat_resp = client.post(
            "/api/jobs",
            json={
                "prompt": "hello",
                "provider": "claude",
                "work_dir": cwd,
            },
        )
        assert chat_resp.status_code == 201, chat_resp.text
        chat_job_id = chat_resp.json()["job_id"]

        # Goal
        goal_resp = client.post(
            "/api/goal/submit",
            json={
                "goal": "write docs",
                "provider": "claude",
                "dir": cwd,
                "cwd": cwd,
            },
        )
        assert goal_resp.status_code in (200, 201), goal_resp.text
        goal_job_id = goal_resp.json()["root_job_id"]

        # SOP
        spec = {
            "name": "simple",
            "tasks": [
                {
                    "name": "step1",
                    "provider": "claude",
                    "input": "do it",
                    "output": "result",
                }
            ],
        }
        sop_resp = client.post(
            "/api/sop/submit",
            json={"spec": spec, "cwd": cwd},
        )
        assert sop_resp.status_code == 200, sop_resp.text
        sop_job_id = sop_resp.json()["root_job_id"]

        # Chat runs are session-indexed; seed the session pointer.
        async def _seed_session() -> None:
            from agt.server.models import SessionModel

            async with _factory() as session:
                session.add(
                    SessionModel(
                        job_id=chat_job_id,
                        identity_did="",
                        provider="claude",
                        session_id=f"sess-{chat_job_id}",
                        cwd=cwd,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                await session.commit()

        asyncio.run(_seed_session())

        # Another chat in different cwd should not appear.
        other_resp = client.post(
            "/api/jobs",
            json={"prompt": "other", "provider": "claude", "work_dir": other_cwd},
        )
        other_job_id = other_resp.json()["job_id"]

        async def _seed_other_session() -> None:
            from agt.server.models import SessionModel

            async with _factory() as session:
                session.add(
                    SessionModel(
                        job_id=other_job_id,
                        identity_did="",
                        provider="claude",
                        session_id=f"sess-{other_job_id}",
                        cwd=other_cwd,
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                await session.commit()

        asyncio.run(_seed_other_session())

        # Unified list for project-x
        runs_resp = client.get("/api/runs", params={"cwd": cwd, "limit": 100})
        assert runs_resp.status_code == 200, runs_resp.text
        data = runs_resp.json()
        modes = {r["mode"]: r["root_job_id"] for r in data["runs"]}
        assert "chat" in modes
        assert "goal" in modes
        assert "sop" in modes
        assert modes["chat"] == chat_job_id
        assert modes["goal"] == goal_job_id
        assert modes["sop"] == sop_job_id
        assert data["total"] == 3

        # Mode filter
        goal_only = client.get("/api/runs", params={"cwd": cwd, "mode": "goal"})
        assert goal_only.json()["total"] == 1
        assert goal_only.json()["runs"][0]["root_job_id"] == goal_job_id

        # Daemon filter (chat job is free -> target_daemon_id None, so filter misses)
        daemon_only = client.get(
            "/api/runs", params={"cwd": cwd, "mode": "chat", "daemon_id": "local"}
        )
        assert daemon_only.json()["total"] == 0
    finally:
        dispose()


def test_job_types_written_on_submit(tmp_path: Path) -> None:
    client, _factory, dispose = _build_app(tmp_path)
    try:
        _register_claude(client)
        cwd = str(tmp_path)

        goal_id = client.post(
            "/api/goal/submit",
            json={
                "goal": "x",
                "provider": "claude",
                "dir": cwd,
                "cwd": cwd,
            },
        ).json()["root_job_id"]

        sop_id = client.post(
            "/api/sop/submit",
            json={
                "spec": {
                    "name": "x",
                    "tasks": [
                        {"name": "a", "provider": "claude", "input": "i", "output": "o"}
                    ],
                },
                "cwd": cwd,
            },
        ).json()["root_job_id"]

        async def _check() -> None:
            async with _factory() as session:
                from agt.server.models import JobModel

                for job_id, expected in ((goal_id, "goal"), (sop_id, "sop")):
                    job = await session.get(JobModel, job_id)
                    assert job is not None
                    assert job.job_type == expected, (job_id, job.job_type)
                    assert job.cwd == cwd, (job_id, job.cwd)

        asyncio.run(_check())
    finally:
        dispose()


def test_goal_cwd_is_independent_from_execution_dir(tmp_path: Path) -> None:
    client, factory, dispose = _build_app(tmp_path)
    try:
        _register_claude(client)
        project_cwd = str(tmp_path / "project")
        daemon_dir = str(tmp_path / "remote-workdir")

        response = client.post(
            "/api/goal/submit",
            json={
                "goal": "separate directories",
                "provider": "claude",
                "dir": daemon_dir,
                "cwd": project_cwd,
            },
        )
        assert response.status_code == 201, response.text
        root_id = response.json()["root_job_id"]

        async def _check() -> None:
            from agt.server.models import JobModel

            async with factory() as session:
                job = await session.get(JobModel, root_id)
                assert job is not None
                assert job.cwd == project_cwd
                assert job.agent_cwd == daemon_dir

        asyncio.run(_check())
        listed = client.get(
            "/api/runs", params={"mode": "goal", "cwd": project_cwd}
        ).json()
        assert listed["total"] == 1
        assert listed["runs"][0]["root_job_id"] == root_id
        assert listed["runs"][0]["cwd"] == project_cwd
    finally:
        dispose()


def _seed_chat_session(factory, job_id: str, cwd: str) -> None:
    async def _insert() -> None:
        from agt.server.models import SessionModel

        async with factory() as session:
            session.add(
                SessionModel(
                    job_id=job_id,
                    identity_did="",
                    provider="claude",
                    session_id=f"sess-{job_id}",
                    cwd=cwd,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            await session.commit()

    asyncio.run(_insert())


def test_chat_cwd_is_independent_from_agent_cwd(tmp_path: Path) -> None:
    """A remote chat records the project path as its run-history cwd."""
    client, factory, dispose = _build_app(tmp_path)
    try:
        _register_claude(client, "remote-daemon")
        project_cwd = str(tmp_path / "project")
        daemon_dir = "/srv/remote-workdir"

        response = client.post(
            "/api/jobs",
            json={
                "prompt": "remote turn",
                "provider": "claude",
                "target_daemon_id": "remote-daemon",
                "agent_cwd": daemon_dir,
                "cwd": project_cwd,
            },
        )
        assert response.status_code == 201, response.text
        job_id = response.json()["job_id"]
        _seed_chat_session(factory, job_id, project_cwd)

        async def _check() -> None:
            from agt.server.models import JobModel

            async with factory() as session:
                job = await session.get(JobModel, job_id)
                assert job is not None
                assert job.cwd == project_cwd
                assert job.agent_cwd == daemon_dir
                assert job.target_daemon_id == "remote-daemon"

        asyncio.run(_check())
        listed = client.get(
            "/api/runs", params={"mode": "chat", "cwd": project_cwd}
        ).json()
        assert listed["total"] == 1
        assert listed["runs"][0]["root_job_id"] == job_id
    finally:
        dispose()


def test_runs_accept_repeated_cwd(tmp_path: Path) -> None:
    """Repeated ``cwd`` values merge project and legacy remote-directory runs."""
    client, factory, dispose = _build_app(tmp_path)
    try:
        _register_claude(client, "remote-daemon")
        project_cwd = str(tmp_path / "project")
        legacy_cwd = "/srv/legacy-workdir"

        current = client.post(
            "/api/jobs",
            json={
                "prompt": "current",
                "provider": "claude",
                "target_daemon_id": "remote-daemon",
                "agent_cwd": legacy_cwd,
                "cwd": project_cwd,
            },
        ).json()["job_id"]
        # Legacy run: no explicit project cwd, so agent_cwd became the key.
        legacy = client.post(
            "/api/jobs",
            json={
                "prompt": "legacy",
                "provider": "claude",
                "target_daemon_id": "remote-daemon",
                "agent_cwd": legacy_cwd,
            },
        ).json()["job_id"]
        _seed_chat_session(factory, current, project_cwd)
        _seed_chat_session(factory, legacy, legacy_cwd)

        params = [("mode", "chat"), ("cwd", project_cwd), ("cwd", legacy_cwd)]
        merged = client.get("/api/runs", params=params).json()
        assert merged["total"] == 2
        assert {run["root_job_id"] for run in merged["runs"]} == {current, legacy}

        only_project = client.get(
            "/api/runs", params={"mode": "chat", "cwd": project_cwd}
        ).json()
        assert only_project["total"] == 1
        assert only_project["runs"][0]["root_job_id"] == current
    finally:
        dispose()
