"""Browser-console hosting and workspace-file API tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.models import Base


SECRET = "web-console-test-secret-0123456789"


def _app(tmp_path: Path, *, host: str = "127.0.0.1", enabled: bool = True):
    assets = tmp_path / "console-assets"
    assets.mkdir()
    (assets / "webview-console-chat.js").write_text("console.log('ok')", encoding="utf-8")
    (assets / "webview-console-chat.css").write_text("body{}", encoding="utf-8")
    for page in ("settings", "goal", "sop"):
        (assets / f"webview-console-{page}.js").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    cfg = ServerConfig(
        host=host,
        port=8420,
        db_path=str(tmp_path / "server.db"),
        sessions_json_path=str(tmp_path / "sessions.json"),
        secret=SECRET,
        console_enabled=enabled,
        console_assets_dir=str(assets),
        console_workspace_roots=[str(workspace)],
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    return create_app(cfg), engine, workspace


def test_console_redirects_pages_assets_and_bootstrap(tmp_path) -> None:
    app, engine, workspace = _app(tmp_path)
    try:
        with TestClient(app, follow_redirects=False) as client:
            root = client.get("/")
            assert root.status_code == 302
            assert root.headers["location"] == "/console/"

            index = client.get("/console/")
            assert index.status_code == 302
            assert index.headers["location"] == "/console/projects"

            page = client.get("/console/projects/test-project/chat")
            assert page.status_code == 200
            assert "/console/assets/webview-console-chat.js" in page.text
            assert "frame-ancestors 'none'" in page.headers["content-security-policy"]

            asset = client.get("/console/assets/webview-console-chat.js")
            assert asset.status_code == 200
            assert asset.text == "console.log('ok')"

            bootstrap = client.get("/api/console/bootstrap")
            assert bootstrap.status_code == 200
            assert bootstrap.json()["workspace_roots"] == [str(workspace.resolve())]
            assert bootstrap.json()["capabilities"]["lifecycle_control"] is True
            assert bootstrap.json()["capabilities"]["sop_files"] is True
    finally:
        asyncio.run(engine.dispose())


def test_console_sop_file_and_traversal_guard(tmp_path) -> None:
    app, engine, workspace = _app(tmp_path)
    target = workspace / "pipeline.sop.yaml"
    try:
        with TestClient(app) as client:
            written = client.put(
                "/api/console/sop/file",
                json={
                    "path": str(target),
                    "text": "name: local\ntasks:\n  - name: one\n    input: test\n",
                },
            )
            assert written.status_code == 200
            assert target.exists()

            templates = client.get(
                "/api/console/sop/templates",
                params={"root": str(workspace)},
            )
            assert templates.status_code == 200
            assert templates.json()["templates"][0]["path"] == str(target.resolve())

            read = client.get(
                "/api/console/sop/file",
                params={"path": str(target)},
            )
            assert read.status_code == 200
            assert "name: local" in read.json()["text"]

            escaped = client.get(
                "/api/console/sop/file",
                params={"path": str(tmp_path / "outside.sop.yaml")},
            )
            assert escaped.status_code == 403
    finally:
        asyncio.run(engine.dispose())


def test_console_disabled_for_opt_out_and_non_loopback(tmp_path) -> None:
    disabled_app, disabled_engine, _ = _app(tmp_path, enabled=False)
    (tmp_path / "remote").mkdir()
    remote_app, remote_engine, _ = _app(tmp_path / "remote", host="0.0.0.0")
    try:
        with TestClient(disabled_app) as client:
            assert client.get("/").status_code == 404
        with TestClient(remote_app) as client:
            assert client.get("/").status_code == 404
    finally:
        asyncio.run(disabled_engine.dispose())
        asyncio.run(remote_engine.dispose())


def test_sop_validate_endpoint_remains_browser_safe(tmp_path) -> None:
    app, engine, _ = _app(tmp_path)
    try:
        with TestClient(app) as client:
            response = client.post(
                "/api/sop/validate",
                json={
                    "raw": "name: browser\ntasks:\n  - name: one\n    input: test\n",
                },
            )
            assert response.status_code == 200
            assert response.json()["ok"] is True
            assert response.json()["nodes"][0]["name"] == "one"
    finally:
        asyncio.run(engine.dispose())


def test_chat_runs_prefer_provider_title_over_first_prompt(tmp_path) -> None:
    app, engine, workspace = _app(tmp_path)
    factory = make_session_factory(engine)
    first_prompt = "first question\nwith additional detail"
    try:
        with TestClient(app) as client:
            created = client.post(
                "/api/jobs",
                json={
                    "prompt": first_prompt,
                    "provider": "claude",
                    "work_dir": str(workspace),
                },
            )
            assert created.status_code == 201, created.text
            job_id = created.json()["job_id"]

            initial_list = client.get(
                "/api/runs",
                params={"mode": "chat", "cwd": str(workspace.resolve())},
            )
            assert initial_list.status_code == 200, initial_list.text
            assert initial_list.json()["runs"][0]["title"] == first_prompt

            async def _seed_second_turn_and_session() -> None:
                from agt.server.models import JobModel, SessionModel
                from agt.server.services.job_service import append_step

                async with factory() as session:
                    job = await session.get(JobModel, job_id)
                    assert job is not None
                    assert job.first_prompt == first_prompt
                    job.title = "Provider native title"
                    job.status = "waiting_input"
                    await append_step(session, job_id, "second question")
                    session.add(
                        SessionModel(
                            job_id=job_id,
                            identity_did="",
                            provider="claude",
                            session_id=f"sess-{job_id}",
                            cwd=str(workspace.resolve()),
                            created_at="2026-10-28 00:00:00",
                            updated_at="2026-10-28 00:00:00",
                        )
                    )
                    await session.commit()

            asyncio.run(_seed_second_turn_and_session())

            listed = client.get(
                "/api/runs",
                params={"mode": "chat", "cwd": str(workspace.resolve())},
            )
            assert listed.status_code == 200, listed.text
            runs = listed.json()["runs"]
            assert len(runs) == 1
            assert runs[0]["title"] == "Provider native title"
    finally:
        asyncio.run(engine.dispose())
