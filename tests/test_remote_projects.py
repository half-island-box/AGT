"""Remote project directories: daemon ``fs.list`` + console project API."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agt.comm.errors import RPCRejected, WSRPCUnavailable
from agt.daemon.ws_server import DaemonWsServer
from agt.server.config import ServerConfig
from agt.server.database import make_engine
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "remote-projects-test-secret-0123456789"
DAEMON_ID = "daemon-remote-01"
DAEMON_URL = "ws://127.0.0.1:8422"


# ── daemon-side fs.list ─────────────────────────────────────────────


def test_fs_list_lists_directories_under_root(tmp_path: Path) -> None:
    root = tmp_path / "home"
    (root / "alpha").mkdir(parents=True)
    (root / "beta").mkdir()
    (root / "note.txt").write_text("not a directory", encoding="utf-8")

    server = DaemonWsServer(
        port=0,
        secret=SECRET,
        daemon_id=DAEMON_ID,
        url=DAEMON_URL,
        allowlist=[root],
    )
    listing = server._list_directory(None)  # noqa: SLF001 — pure helper

    assert listing["current"] == str(root.resolve())
    assert listing["roots"] == [str(root.resolve())]
    assert listing["parent"] is None  # root has no browsable parent
    assert [item["name"] for item in listing["directories"]] == ["alpha", "beta"]


def test_fs_list_rejects_paths_outside_allowlist(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    server = DaemonWsServer(
        port=0,
        secret=SECRET,
        daemon_id=DAEMON_ID,
        url=DAEMON_URL,
        allowlist=[root],
    )
    with pytest.raises(ValueError) as excinfo:
        server._list_directory(str(outside))  # noqa: SLF001
    assert excinfo.value.args[0]["reason"] == "path_outside_roots"


def test_fs_list_rejects_relative_and_missing_paths(tmp_path: Path) -> None:
    root = tmp_path / "home"
    root.mkdir()
    server = DaemonWsServer(
        port=0,
        secret=SECRET,
        daemon_id=DAEMON_ID,
        url=DAEMON_URL,
        allowlist=[root],
    )
    with pytest.raises(ValueError) as excinfo:
        server._list_directory("relative/dir")  # noqa: SLF001
    assert excinfo.value.args[0]["reason"] == "relative_path"

    with pytest.raises(ValueError) as excinfo:
        server._list_directory(str(root / "missing"))  # noqa: SLF001
    assert excinfo.value.args[0]["reason"] == "not_a_directory"


def test_fs_list_parent_clamps_at_root(tmp_path: Path) -> None:
    root = tmp_path / "home"
    child = root / "project"
    child.mkdir(parents=True)

    server = DaemonWsServer(
        port=0,
        secret=SECRET,
        daemon_id=DAEMON_ID,
        url=DAEMON_URL,
        allowlist=[root],
    )
    listing = server._list_directory(str(child))  # noqa: SLF001
    assert listing["parent"] == str(root.resolve())


# ── end-to-end: DaemonLink → DaemonWsServer ``fs.list`` ────────────


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_daemon_link_lists_remote_directories(tmp_path: Path) -> None:
    root = tmp_path / "remote-home"
    (root / "workspace").mkdir(parents=True)

    async def _run() -> None:
        from agt.server.daemon_link import DaemonLink

        port = _free_port()
        ws_server = DaemonWsServer(
            port=port,
            secret=SECRET,
            daemon_id=DAEMON_ID,
            url=f"ws://127.0.0.1:{port}",
            allowlist=[root],
        )
        await ws_server.start()
        shutdown = asyncio.Event()
        link = DaemonLink(
            daemon_id=DAEMON_ID,
            url=f"ws://127.0.0.1:{port}",
            secret=SECRET,
            session_factory=None,
        )
        link_task = asyncio.create_task(link.run(shutdown))
        try:
            for _ in range(60):
                if link.connected and link.supports_rpc_v1:
                    break
                await asyncio.sleep(0.05)
            assert link.connected, "DaemonLink did not connect"

            listing = await link.list_directory(None)
            assert listing["current"] == str(root.resolve())
            assert [item["name"] for item in listing["directories"]] == ["workspace"]

            with pytest.raises(RPCRejected) as excinfo:
                await link.list_directory(str(tmp_path / "outside"))
            assert excinfo.value.body["reason"] == "path_outside_roots"
        finally:
            shutdown.set()
            link_task.cancel()
            try:
                await asyncio.wait_for(link_task, timeout=2)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
            await ws_server.stop()

    asyncio.run(_run())


# ── server-side console API ────────────────────────────────────────


class _FakeLink:
    """Minimal ``DaemonLink`` stand-in for route tests."""

    def __init__(self, *, listing=None, error: Exception | None = None) -> None:
        self.listing = listing
        self.error = error
        self.calls: list[str | None] = []

    async def list_directory(self, path: str | None) -> dict:
        self.calls.append(path)
        if self.error is not None:
            raise self.error
        assert self.listing is not None
        return self.listing


def _build_app(tmp_path: Path) -> tuple[TestClient, Path]:
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        sessions_json_path=str(tmp_path / "sessions.json"),
        secret=SECRET,
        console_enabled=True,
        console_allow_remote=False,
        console_assets_dir=str(tmp_path / "assets"),
        console_workspace_roots=[str(workspace)],
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    return TestClient(create_app(cfg)), workspace


_REMOTE_LISTING = {
    "roots": ["/home/remote"],
    "parent": "/home",
    "current": "/home/remote/code",
    "directories": [{"name": "api", "path": "/home/remote/code/api"}],
}


def test_directories_with_daemon_id_uses_reverse_dial_link(tmp_path: Path) -> None:
    client, _workspace = _build_app(tmp_path)
    link = _FakeLink(listing=_REMOTE_LISTING)
    client.app.state.daemon_links = {DAEMON_ID: link}

    response = client.get(
        "/api/console/directories",
        params={"daemon_id": DAEMON_ID, "path": "/home/remote/code"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["current"] == "/home/remote/code"
    assert body["directories"] == [{"name": "api", "path": "/home/remote/code/api"}]
    assert link.calls == ["/home/remote/code"]


def test_directories_unknown_daemon_is_404(tmp_path: Path) -> None:
    client, _workspace = _build_app(tmp_path)
    client.app.state.daemon_links = {}

    response = client.get(
        "/api/console/directories", params={"daemon_id": "nope"}
    )

    assert response.status_code == 404
    assert response.json()["detail"]["type"] == "unknown_remote_daemon"


def test_directories_offline_daemon_is_503(tmp_path: Path) -> None:
    client, _workspace = _build_app(tmp_path)
    client.app.state.daemon_links = {
        DAEMON_ID: _FakeLink(error=WSRPCUnavailable("daemon offline"))
    }

    response = client.get(
        "/api/console/directories", params={"daemon_id": DAEMON_ID}
    )

    assert response.status_code == 503
    assert response.json()["detail"]["type"] == "daemon_offline"


def test_create_project_with_remote_directory(tmp_path: Path) -> None:
    client, workspace = _build_app(tmp_path)
    local = workspace / "local-project"
    local.mkdir()
    link = _FakeLink(listing=_REMOTE_LISTING)
    client.app.state.daemon_links = {DAEMON_ID: link}

    response = client.post(
        "/api/console/projects",
        json={
            "path": str(local),
            "name": "Dual",
            "remote_daemon_id": DAEMON_ID,
            "remote_path": "/home/remote/code",
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["path"] == str(local.resolve())
    assert body["remote_daemon_id"] == DAEMON_ID
    assert body["remote_path"] == "/home/remote/code"
    assert body["remote"] is True
    # The route validated the remote path through the daemon.
    assert link.calls == ["/home/remote/code"]


def test_create_project_remote_pair_must_be_complete(tmp_path: Path) -> None:
    client, workspace = _build_app(tmp_path)
    local = workspace / "local-project"
    local.mkdir()

    response = client.post(
        "/api/console/projects",
        json={"path": str(local), "remote_daemon_id": DAEMON_ID},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "remote_pair_incomplete"


def test_create_project_rejects_relative_remote_path(tmp_path: Path) -> None:
    client, workspace = _build_app(tmp_path)
    local = workspace / "local-project"
    local.mkdir()
    client.app.state.daemon_links = {DAEMON_ID: _FakeLink(listing=_REMOTE_LISTING)}

    response = client.post(
        "/api/console/projects",
        json={
            "path": str(local),
            "remote_daemon_id": DAEMON_ID,
            "remote_path": "relative/dir",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "remote_path_not_absolute"


def test_create_project_rejects_remote_path_outside_daemon_allowlist(
    tmp_path: Path,
) -> None:
    client, workspace = _build_app(tmp_path)
    local = workspace / "local-project"
    local.mkdir()
    rejected = RPCRejected(
        0,
        {
            "type": "path_error",
            "reason": "path_outside_roots",
            "message": "path is outside the daemon's agent_cwd_allowlist",
        },
    )
    client.app.state.daemon_links = {DAEMON_ID: _FakeLink(error=rejected)}

    response = client.post(
        "/api/console/projects",
        json={
            "path": str(local),
            "remote_daemon_id": DAEMON_ID,
            "remote_path": "/etc",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["type"] == "path_error"


def test_create_project_tolerates_offline_daemon(tmp_path: Path) -> None:
    client, workspace = _build_app(tmp_path)
    local = workspace / "local-project"
    local.mkdir()
    client.app.state.daemon_links = {
        DAEMON_ID: _FakeLink(error=WSRPCUnavailable("daemon offline"))
    }

    response = client.post(
        "/api/console/projects",
        json={
            "path": str(local),
            "remote_daemon_id": DAEMON_ID,
            "remote_path": "/home/remote/code",
        },
    )

    assert response.status_code == 201
    assert response.json()["remote"] is True


def test_remote_project_directory_pair_is_unique(tmp_path: Path) -> None:
    client, workspace = _build_app(tmp_path)
    first = workspace / "first"
    second = workspace / "second"
    first.mkdir()
    second.mkdir()
    client.app.state.daemon_links = {DAEMON_ID: _FakeLink(listing=_REMOTE_LISTING)}

    payload = {
        "remote_daemon_id": DAEMON_ID,
        "remote_path": "/home/remote/code",
    }
    assert client.post(
        "/api/console/projects",
        json={"path": str(first), **payload},
    ).status_code == 201

    duplicate = client.post(
        "/api/console/projects",
        json={"path": str(second), **payload},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"]["type"] == "remote_project_already_exists"


# ── legacy DB backfill ─────────────────────────────────────────────


def test_ensure_schema_backfills_console_project_remote_columns(
    tmp_path: Path,
) -> None:
    from sqlalchemy import create_engine, inspect, text

    from agt.server.migrate_schema import ensure_schema

    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        # A console_projects table from before the remote columns existed.
        conn.exec_driver_sql(
            """
            CREATE TABLE console_projects (
                id VARCHAR(64) NOT NULL PRIMARY KEY,
                name VARCHAR(128) NOT NULL,
                path VARCHAR(1024) NOT NULL UNIQUE,
                created_at VARCHAR(32) NOT NULL,
                updated_at VARCHAR(32) NOT NULL,
                last_opened_at VARCHAR(32)
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO console_projects (id, name, path, created_at, updated_at) "
            "VALUES ('project-old', 'Old', '/tmp/old', '2026-01-01', '2026-01-01')"
        )
        ensure_schema(conn)
        # Idempotent: a second pass must not fail on the existing columns.
        ensure_schema(conn)

        columns = {
            row[1] for row in conn.exec_driver_sql(
                "PRAGMA table_info(console_projects)"
            )
        }
        assert {"remote_daemon_id", "remote_path"} <= columns
        remaining = conn.exec_driver_sql(
            "SELECT remote_daemon_id, remote_path FROM console_projects"
        ).fetchone()
        assert remaining == (None, None)

        indexes = {item["name"] for item in inspect(conn).get_indexes("console_projects")}
        assert "ux_console_projects_remote" in indexes
