"""``fs.list`` — remote-daemon directory browsing for the plugin's workdir picker.

Covers the two halves of the feature:

- ``DaemonWsServer._list_directory`` — the daemon-side browser. Roots are the
  daemon's ``agent_cwd_allowlist``; anything selectable is therefore also a
  path ``Daemon.run`` will accept. Enforcement (``path_outside_roots``,
  ``relative_path``) is the security-sensitive part.
- ``GET /api/daemons/{id}/directories`` — the Server proxy that turns a
  reverse-dial ``DaemonLink.list_directory`` into an HTTP response.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agt.comm.errors import WSRPCUnavailable
from agt.daemon.ws_server import DaemonWsServer
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base

SECRET = "fs-list-test-secret"


# ── Daemon-side browser ─────────────────────────────────────────────


def _server(allowlist: list[Path] | None) -> DaemonWsServer:
    return DaemonWsServer(
        port=0, secret="x", daemon_id="d1", url="ws://example",
        allowlist=allowlist,
    )


def _make_tree(root: Path) -> None:
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    (root / "gamma").mkdir(parents=True, exist_ok=True)
    (root / "nested").mkdir()
    (root / "nested" / "deep").mkdir()
    (root / "file.txt").write_text("not a directory")


class TestListDirectory:
    def test_roots_and_directories(self, tmp_path) -> None:
        _make_tree(tmp_path)
        result = _server([tmp_path])._list_directory(None)

        assert result["roots"] == [str(tmp_path)]
        assert result["current"] == str(tmp_path)
        assert result["parent"] is None  # tmp_path.parent is outside the root
        assert result["directories"] == [
            {"name": "alpha", "path": str(tmp_path / "alpha")},
            {"name": "beta", "path": str(tmp_path / "beta")},
            {"name": "gamma", "path": str(tmp_path / "gamma")},
            {"name": "nested", "path": str(tmp_path / "nested")},
        ]

    def test_files_are_skipped(self, tmp_path) -> None:
        (tmp_path / "file.txt").write_text("x")
        result = _server([tmp_path])._list_directory(None)
        names = [item["name"] for item in result["directories"]]
        assert "file.txt" not in names

    def test_parent_points_back_into_root(self, tmp_path) -> None:
        _make_tree(tmp_path)
        nested = tmp_path / "nested"
        result = _server([tmp_path])._list_directory(str(nested))
        assert result["current"] == str(nested)
        assert result["parent"] == str(tmp_path)
        assert result["directories"] == [
            {"name": "deep", "path": str(nested / "deep")},
        ]

    def test_path_outside_roots_rejected(self, tmp_path) -> None:
        _make_tree(tmp_path)
        server = _server([tmp_path])
        with pytest.raises(ValueError, match="path_outside_roots"):
            server._list_directory("/")

    def test_relative_path_rejected(self, tmp_path) -> None:
        server = _server([tmp_path])
        with pytest.raises(ValueError, match="relative_path"):
            server._list_directory("relative/path")

    def test_blank_path_rejected(self, tmp_path) -> None:
        server = _server([tmp_path])
        with pytest.raises(ValueError, match="path_required"):
            server._list_directory("   ")


class TestHandleFsList:
    def test_non_string_path_rejected(self, tmp_path) -> None:
        server = _server([tmp_path])

        async def _run() -> None:
            with pytest.raises(ValueError, match="validation_error"):
                await server._handle_fs_list({"path": 42})

        asyncio.run(_run())


# ── Server proxy route ──────────────────────────────────────────────


def _build_app(tmp_path):
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "server.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
        daemons=[],
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

    return client, _dispose


class _FakeDaemonLink:
    def __init__(self, *, result=None, raise_exc=None) -> None:
        self._result = result if result is not None else {}
        self._raise_exc = raise_exc
        self.calls: list[str | None] = []

    async def list_directory(self, path: str | None) -> dict:
        self.calls.append(path)
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


class TestDaemonDirectoriesRoute:
    def test_unknown_daemon_404(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/daemons/nope/directories")
            assert resp.status_code == 404
            assert resp.json()["detail"]["type"] == "unknown_remote_daemon"
        finally:
            dispose()

    def test_daemon_offline_503(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            client.app.state.daemon_links = {
                "d1": _FakeDaemonLink(raise_exc=WSRPCUnavailable("down"))
            }
            resp = client.get("/api/daemons/d1/directories")
            assert resp.status_code == 503
            assert resp.json()["detail"]["type"] == "daemon_offline"
        finally:
            dispose()

    def test_forwards_path_and_returns_listing(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            fake = _FakeDaemonLink(result={
                "roots": ["/srv"],
                "parent": None,
                "current": "/srv",
                "directories": [{"name": "proj", "path": "/srv/proj"}],
            })
            client.app.state.daemon_links = {"d1": fake}

            resp = client.get("/api/daemons/d1/directories?path=/srv")
            assert resp.status_code == 200
            assert resp.json() == {
                "roots": ["/srv"],
                "parent": None,
                "current": "/srv",
                "directories": [{"name": "proj", "path": "/srv/proj"}],
            }
            assert fake.calls == ["/srv"]
        finally:
            dispose()
