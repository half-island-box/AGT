"""Phase 4 — daemon registry view route tests (FastAPI TestClient).

Verifies GET /api/daemons and GET /api/daemons/{id}: grouping, offline
visibility, 404, and that the aggregated response shape is correct.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, RuntimeModel
from agt.server.services.runtime_service import TS_FMT


SECRET = "daemon-routes-test-secret"


def _stamp(age_seconds: float) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    return (now - timedelta(seconds=age_seconds)).strftime(TS_FMT)


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "daemons.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())

    async def _seed() -> None:
        async with factory() as session:
            session.add_all([
                RuntimeModel(
                    daemon_id="d1", provider="claude",
                    name="claude (d1)", version="2.1.0", status="online",
                    device_name="laptop-a", last_seen=_stamp(1),
                ),
                RuntimeModel(
                    daemon_id="d1", provider="codex",
                    name="codex (d1)", version="1.0.0", status="online",
                    device_name="laptop-a", last_seen=_stamp(2),
                ),
                RuntimeModel(
                    daemon_id="d2", provider="codex",
                    name="codex (d2)", version="0.9.0", status="online",
                    device_name="old-box", last_seen=_stamp(60_000),  # stale → offline
                ),
            ])
            await session.commit()

    asyncio.run(_seed())

    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return client, _dispose


class TestList:
    def test_groups_by_daemon(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/daemons")
            assert resp.status_code == 200
            daemons = resp.json()["daemons"]
            by_id = {d["daemon_id"]: d for d in daemons}
            assert set(by_id) == {"d1", "d2"}

            d1 = by_id["d1"]
            assert d1["runtime_count"] == 2
            assert {p["provider"] for p in d1["providers"]} == {"claude", "codex"}
            assert d1["online"] is True
            assert d1["device_name"] == "laptop-a"
        finally:
            dispose()

    def test_offline_daemon_visible_with_heartbeat(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            d2 = next(d for d in client.get("/api/daemons").json()["daemons"]
                      if d["daemon_id"] == "d2")
            assert d2["online"] is False
            assert d2["last_seen"] != ""
        finally:
            dispose()


class TestGet:
    def test_get_known_daemon(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/daemons/d1")
            assert resp.status_code == 200
            body = resp.json()
            assert body["daemon_id"] == "d1"
            assert len(body["providers"]) == 2
        finally:
            dispose()

    def test_get_unknown_404(self, tmp_path) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/daemons/no-such-daemon")
            assert resp.status_code == 404
        finally:
            dispose()
