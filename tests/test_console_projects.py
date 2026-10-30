"""Browser-console project persistence and directory browsing tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, ConsoleProjectModel, JobModel


SECRET = "console-projects-test-secret-0123456789"


def _build_app(
    tmp_path: Path,
    *,
    host: str = "127.0.0.1",
    enabled: bool = True,
    allow_remote: bool = False,
) -> tuple[TestClient, callable, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    assets = tmp_path / "console-assets"
    assets.mkdir()
    for page in ("chat", "goal", "projects", "settings", "sop"):
        (assets / f"webview-console-{page}.js").write_text(
            f"console.log({page!r})",
            encoding="utf-8",
        )
        (assets / f"webview-console-{page}.css").write_text(
            "body{}",
            encoding="utf-8",
        )
    cfg = ServerConfig(
        host=host,
        port=8420,
        db_path=str(tmp_path / "server.db"),
        sessions_json_path=str(tmp_path / "sessions.json"),
        secret=SECRET,
        console_enabled=enabled,
        console_allow_remote=allow_remote,
        console_assets_dir=str(assets),
        console_workspace_roots=[str(workspace)],
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return TestClient(create_app(cfg)), _dispose, workspace


def _add_runs(engine, cwd: str, prefix: str = "") -> None:
    factory = make_session_factory(engine)

    async def _insert() -> None:
        async with factory() as db:
            for index, mode in enumerate(("chat", "sop", "goal", "task")):
                db.add(
                    JobModel(
                        job_id=f"{prefix}job-{index}",
                        root_job_id=f"{prefix}job-{index}",
                        status="done",
                        job_type=mode,
                        provider="claude",
                        cwd=cwd,
                        created_at="2026-01-01T00:00:00",
                        updated_at="2026-01-01T00:00:00",
                    )
                )
            await db.commit()

    asyncio.run(_insert())


def test_project_crud_lifecycle_and_run_counts(tmp_path: Path) -> None:
    client, dispose, workspace = _build_app(tmp_path)
    project_dir = workspace / "agent-team"
    project_dir.mkdir()
    try:
        empty = client.get("/api/console/projects")
        assert empty.status_code == 200
        assert empty.json()["projects"] == []

        created = client.post(
            "/api/console/projects",
            json={"path": str(project_dir), "name": "My project"},
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["name"] == "My project"
        assert body["path"] == str(project_dir.resolve())
        assert body["available"] is True
        assert body["run_counts"] == {"chat": 0, "sop": 0, "goal": 0}
        project_id = body["id"]

        duplicate = client.post(
            "/api/console/projects",
            json={"path": str(project_dir / ".." / "agent-team")},
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["detail"]["type"] == "project_already_exists"

        _add_runs(client.app.state.session_factory().bind, body["path"])
        listed = client.get("/api/console/projects").json()["projects"]
        assert listed[0]["run_counts"] == {
            "chat": 1,
            "sop": 1,
            "goal": 1,
        }

        renamed = client.patch(
            f"/api/console/projects/{project_id}",
            json={"name": "Renamed"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Renamed"

        opened = client.post(f"/api/console/projects/{project_id}/open")
        assert opened.status_code == 200
        assert opened.json()["last_opened_at"] is not None

        deleted = client.delete(f"/api/console/projects/{project_id}")
        assert deleted.status_code == 204
        assert project_dir.is_dir()
        assert client.get("/api/console/projects").json()["projects"] == []
    finally:
        dispose()


def _add_project(
    engine,
    *,
    project_id: str,
    path: str,
    remote_daemon_id: str | None = None,
    remote_path: str | None = None,
) -> None:
    factory = make_session_factory(engine)

    async def _insert() -> None:
        async with factory() as db:
            db.add(
                ConsoleProjectModel(
                    id=project_id,
                    name="Remote project",
                    path=path,
                    remote_daemon_id=remote_daemon_id,
                    remote_path=remote_path,
                    created_at="2026-01-01T00:00:00",
                    updated_at="2026-01-01T00:00:00",
                )
            )
            await db.commit()

    asyncio.run(_insert())


def test_run_counts_merge_legacy_remote_directory(tmp_path: Path) -> None:
    """Runs recorded under the remote directory still count for the project."""
    client, dispose, workspace = _build_app(tmp_path)
    project_dir = workspace / "remote-project"
    project_dir.mkdir()
    try:
        engine = client.app.state.session_factory().bind
        _add_project(
            engine,
            project_id="project-remote",
            path=str(project_dir),
            remote_daemon_id="dev-box-2",
            remote_path="/srv/remote-project",
        )
        # Local runs use the project path; legacy chat runs used the remote
        # directory as their run-history cwd.
        _add_runs(engine, str(project_dir), prefix="local-")
        _add_runs(engine, "/srv/remote-project", prefix="remote-")

        listed = client.get("/api/console/projects").json()["projects"]
        assert listed[0]["run_counts"] == {"chat": 2, "sop": 2, "goal": 2}

        detail = client.get("/api/console/projects/project-remote").json()
        assert detail["run_counts"] == {"chat": 2, "sop": 2, "goal": 2}
    finally:
        dispose()


def test_project_path_validation_and_symlink_escape(tmp_path: Path) -> None:
    client, dispose, workspace = _build_app(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    inside_link = workspace / "escape"
    inside_link.symlink_to(outside, target_is_directory=True)
    try:
        relative = client.post(
            "/api/console/projects",
            json={"path": "relative/path"},
        )
        assert relative.status_code == 400
        assert relative.json()["detail"]["type"] == "relative_path"

        missing = client.post(
            "/api/console/projects",
            json={"path": str(workspace / "missing")},
        )
        assert missing.status_code == 400
        assert missing.json()["detail"]["type"] == "path_not_directory"

        escaped = client.post(
            "/api/console/projects",
            json={"path": str(inside_link)},
        )
        assert escaped.status_code == 403
        assert escaped.json()["detail"]["type"] == "path_outside_roots"
    finally:
        inside_link.unlink(missing_ok=True)
        dispose()


def test_directory_browsing_returns_only_allowed_directories(tmp_path: Path) -> None:
    client, dispose, workspace = _build_app(tmp_path)
    child = workspace / "child"
    nested = child / "nested"
    file = workspace / "file.txt"
    child.mkdir()
    nested.mkdir()
    file.write_text("not a directory", encoding="utf-8")
    try:
        root = client.get("/api/console/directories")
        assert root.status_code == 200
        body = root.json()
        assert body["current"] == str(workspace.resolve())
        assert body["parent"] is None
        assert body["directories"] == [
            {"name": "child", "path": str(child.resolve())}
        ]

        nested_response = client.get(
            "/api/console/directories",
            params={"path": str(child)},
        )
        assert nested_response.status_code == 200
        assert nested_response.json()["parent"] == str(workspace.resolve())
        assert nested_response.json()["directories"] == [
            {"name": "nested", "path": str(nested.resolve())}
        ]

        outside = client.get(
            "/api/console/directories",
            params={"path": str(tmp_path)},
        )
        assert outside.status_code == 403
    finally:
        dispose()


def test_sop_file_rename_is_scoped_and_non_overwriting(tmp_path: Path) -> None:
    client, dispose, workspace = _build_app(tmp_path)
    source = workspace / "review.sop.yaml"
    existing = workspace / "existing.sop.yaml"
    source.write_text(
        "name: review\ntasks:\n  - name: one\n    input: test\n",
        encoding="utf-8",
    )
    existing.write_text("name: existing\n", encoding="utf-8")
    try:
        renamed = client.post(
            "/api/console/sop/file/rename",
            json={
                "path": str(source),
                "new_name": "renamed.sop.yaml",
            },
        )
        assert renamed.status_code == 200
        assert renamed.json()["path"] == str(
            (workspace / "renamed.sop.yaml").resolve()
        )
        assert not source.exists()
        assert (workspace / "renamed.sop.yaml").is_file()

        duplicate = client.post(
            "/api/console/sop/file/rename",
            json={
                "path": str(workspace / "renamed.sop.yaml"),
                "new_name": "existing.sop.yaml",
            },
        )
        assert duplicate.status_code == 409

        traversal = client.post(
            "/api/console/sop/file/rename",
            json={
                "path": str(workspace / "renamed.sop.yaml"),
                "new_name": "../outside.sop.yaml",
            },
        )
        assert traversal.status_code == 400
    finally:
        dispose()


def test_unavailable_project_can_be_listed_but_not_opened(tmp_path: Path) -> None:
    client, dispose, workspace = _build_app(tmp_path)
    target = workspace / "gone"
    target.mkdir()
    created = client.post(
        "/api/console/projects",
        json={"path": str(target)},
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    target.rmdir()
    try:
        listed = client.get("/api/console/projects").json()["projects"]
        assert listed[0]["available"] is False

        opened = client.post(f"/api/console/projects/{project_id}/open")
        assert opened.status_code == 409
        assert opened.json()["detail"]["type"] == "project_unavailable"

        deleted = client.delete(f"/api/console/projects/{project_id}")
        assert deleted.status_code == 204
    finally:
        dispose()


def test_console_project_api_disabled_for_non_loopback_or_opt_out(
    tmp_path: Path,
) -> None:
    loopback, dispose_loopback, _ = _build_app(tmp_path)
    remote, dispose_remote, _ = _build_app(tmp_path / "remote", host="0.0.0.0")
    disabled, dispose_disabled, _ = _build_app(
        tmp_path / "disabled", enabled=False
    )
    try:
        assert loopback.get("/api/console/projects").status_code == 200
        assert remote.get("/api/console/projects").status_code == 404
        assert disabled.get("/api/console/projects").status_code == 404
    finally:
        dispose_loopback()
        dispose_remote()
        dispose_disabled()


def test_console_project_routes_and_assets(tmp_path: Path) -> None:
    client, dispose, _ = _build_app(tmp_path)
    try:
        root = client.get("/", follow_redirects=False)
        assert root.status_code == 302
        assert root.headers["location"] == "/console/"

        index = client.get("/console/", follow_redirects=False)
        assert index.status_code == 302
        assert index.headers["location"] == "/console/projects"

        projects = client.get("/console/projects")
        assert projects.status_code == 200
        assert "/console/assets/webview-console-projects.js" in projects.text

        legacy_chat = client.get("/console/chat", follow_redirects=False)
        assert legacy_chat.status_code == 302
        assert legacy_chat.headers["location"] == "/console/projects"

        project = client.get(
            "/console/projects/project-example",
            follow_redirects=False,
        )
        assert project.status_code == 302
        assert (
            project.headers["location"]
            == "/console/projects/project-example/chat"
        )

        chat = client.get("/console/projects/project-example/chat")
        assert chat.status_code == 200
        assert "/console/assets/webview-console-chat.js" in chat.text

        asset = client.get("/console/assets/webview-console-projects.js")
        assert asset.status_code == 200
        assert "console.log('projects')" in asset.text

        missing_mode = client.get(
            "/console/projects/project-example/settings",
        )
        assert missing_mode.status_code == 404
    finally:
        dispose()
