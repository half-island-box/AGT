"""SOP run-template association and filtering tests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import json

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, JobModel


SECRET = "sop-template-association-test-secret"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _build_app(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "sessions.json"),
        console_workspace_roots=[str(project)],
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    return TestClient(create_app(cfg)), factory, lambda: asyncio.run(engine.dispose()), project


def _register_claude(client: TestClient) -> None:
    response = client.post(
        "/api/runtimes/register",
        json={
            "daemon_id": "local",
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
    assert response.status_code in (200, 201), response.text


def _spec(name: str) -> dict:
    return {
        "name": name,
        "tasks": [
            {
                "name": "step1",
                "provider": "claude",
                "input": "do it",
                "output": "result",
            }
        ],
    }


def _submit(client: TestClient, cwd: str, template_path: Path, name: str) -> str:
    response = client.post(
        "/api/sop/submit",
        json={
            "spec": _spec(name),
            "cwd": cwd,
            "template_path": str(template_path),
            "template_hash": f"hash-{name}",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["root_job_id"]


def test_sop_runs_filter_by_template_and_exclude_legacy_rows(tmp_path: Path) -> None:
    client, factory, dispose, project = _build_app(tmp_path)
    try:
        _register_claude(client)
        template_a = project / "a.sop.yaml"
        template_b = project / "b.sop.yaml"
        template_a.write_text("name: a\n", encoding="utf-8")
        template_b.write_text("name: b\n", encoding="utf-8")

        run_a = _submit(client, str(project), template_a, "a")
        run_b = _submit(client, str(project), template_b, "b")

        async def _seed_legacy() -> None:
            async with factory() as db:
                db.add(
                    JobModel(
                        job_id="legacy-sop",
                        root_job_id="legacy-sop",
                        status="done",
                        job_type="sop",
                        provider="claude",
                        cwd=str(project),
                        sop_node=json.dumps({"name": "legacy"}),
                        created_at=_now(),
                        updated_at=_now(),
                    )
                )
                await db.commit()

        asyncio.run(_seed_legacy())

        all_runs = client.get(
            "/api/runs",
            params={"mode": "sop", "cwd": str(project), "limit": 50},
        )
        assert all_runs.status_code == 200, all_runs.text
        assert all_runs.json()["total"] == 3

        a_runs = client.get(
            "/api/runs",
            params={
                "mode": "sop",
                "cwd": str(project),
                "template_path": str(template_a),
            },
        )
        assert a_runs.status_code == 200, a_runs.text
        assert a_runs.json()["total"] == 1
        run = a_runs.json()["runs"][0]
        assert run["root_job_id"] == run_a
        assert run["template_path"] == str(template_a)
        assert run["template_hash"] == "hash-a"

        b_ids = {
            item["root_job_id"]
            for item in client.get(
                "/api/runs",
                params={
                    "mode": "sop",
                    "cwd": str(project),
                    "template_path": str(template_b),
                },
            ).json()["runs"]
        }
        assert b_ids == {run_b}

        async def _check_rows() -> None:
            async with factory() as db:
                row_a = await db.get(JobModel, run_a)
                row_b = await db.get(JobModel, run_b)
                assert row_a is not None
                assert row_b is not None
                assert row_a.sop_template_path == str(template_a)
                assert row_a.sop_template_hash == "hash-a"
                assert row_b.sop_template_path == str(template_b)

        asyncio.run(_check_rows())
    finally:
        dispose()


def test_console_sop_rename_moves_template_association(tmp_path: Path) -> None:
    client, factory, dispose, project = _build_app(tmp_path)
    try:
        _register_claude(client)
        source = project / "before.sop.yaml"
        destination = project / "after.sop.yaml"
        source.write_text("name: before\n", encoding="utf-8")
        run_id = _submit(client, str(project), source, "before")

        renamed = client.post(
            "/api/console/sop/file/rename",
            json={"path": str(source), "new_name": "after.sop.yaml"},
        )
        assert renamed.status_code == 200, renamed.text
        assert not source.exists()
        assert destination.is_file()

        moved = client.get(
            "/api/runs",
            params={
                "mode": "sop",
                "cwd": str(project),
                "template_path": str(destination),
            },
        )
        assert moved.status_code == 200, moved.text
        assert moved.json()["total"] == 1
        assert moved.json()["runs"][0]["root_job_id"] == run_id

        old = client.get(
            "/api/runs",
            params={
                "mode": "sop",
                "cwd": str(project),
                "template_path": str(source),
            },
        )
        assert old.json()["total"] == 0

        async def _check_hash_preserved() -> None:
            async with factory() as db:
                row = await db.get(JobModel, run_id)
                assert row is not None
                assert row.sop_template_path == str(destination)
                assert row.sop_template_hash == "hash-before"

        asyncio.run(_check_hash_preserved())
    finally:
        dispose()


def test_sop_template_inputs_are_validated(tmp_path: Path) -> None:
    client, _factory, dispose, project = _build_app(tmp_path)
    try:
        _register_claude(client)
        relative = client.post(
            "/api/sop/submit",
            json={
                "spec": _spec("relative"),
                "cwd": str(project),
                "template_path": "relative.sop.yaml",
            },
        )
        assert relative.status_code == 400
        assert "absolute" in relative.json()["detail"]

        hash_without_path = client.post(
            "/api/sop/submit",
            json={
                "spec": _spec("hash-only"),
                "cwd": str(project),
                "template_hash": "hash-only",
            },
        )
        assert hash_without_path.status_code == 400

        template_without_sop_mode = client.get(
            "/api/runs",
            params={"cwd": str(project), "template_path": str(project / "x.yaml")},
        )
        assert template_without_sop_mode.status_code == 400
        assert template_without_sop_mode.json()["detail"] == (
            "template_path requires mode=sop"
        )
    finally:
        dispose()
