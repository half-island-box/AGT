"""Phase 4 Step 1 — identity CRUD route tests (FastAPI TestClient).

Verifies the HTTP surface over the identity service:

- POST /api/identities: 201 + AGENTS.md written under the identities root;
  409 on duplicate DID; 400 on invalid slug.
- GET /api/identities (+daemon_id/status_filter) and /{did} (200/404).
- PUT /{did}: partial update without touching AGENTS.md.
- DELETE /{did}: 204; 404 unknown; 409 when a task references the identity.
- POST /{did}/deprecate, /{did}/revoke, /{did}/sync-files.

AGENTS.md writes are redirected to tmp_path by monkeypatching
``routes.identities._root_dir`` so tests never touch the real ~/.agt.
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, TaskModel


SECRET = "identity-routes-test-secret"


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    """Boot a TestClient on a fresh tmp DB; returns ``(client, dispose)``."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "identities.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)

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


def _payload(**overrides) -> dict:
    base = {
        "daemon_id": "d1",
        "name": "Code Reviewer",
        "slug": "code-reviewer",
        "description": "Python code review",
        "capabilities": ["code-review"],
    }
    base.update(overrides)
    return base


def _add_task_ref(engine, did: str, task_id: str) -> None:
    """Insert a task row referencing ``did`` directly (no API path yet)."""
    factory = make_session_factory(engine)

    async def _insert() -> None:
        async with factory() as session:
            session.add(TaskModel(
                task_id=task_id, provider="claude", prompt="hi",
                status="completed",
                created_at="2026-01-01 00:00:00",
                updated_at="2026-01-01 00:00:00",
                identity_did=did,
            ))
            await session.commit()

    asyncio.run(_insert())


# ── Create ─────────────────────────────────────────────────────────


def test_create_returns_201_and_writes_agent_md(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        resp = client.post("/api/identities", json=_payload())
        assert resp.status_code == 201
        body = resp.json()
        assert body["did"] == "did:agt:code-reviewer"
        assert body["status"] == "active"

        md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        assert md.exists()
        assert "Code Reviewer" in md.read_text()
    finally:
        dispose()


def test_create_duplicate_conflicts(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        assert client.post("/api/identities", json=_payload()).status_code == 201
        dup = client.post("/api/identities", json=_payload())
        assert dup.status_code == 409
        assert "already exists" in dup.json()["detail"]
    finally:
        dispose()


def test_create_invalid_slug_bad_request(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        resp = client.post("/api/identities", json=_payload(slug="Bad_Slug!"))
        assert resp.status_code == 400
    finally:
        dispose()


# ── Read ───────────────────────────────────────────────────────────


def test_list_and_filters(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload())
        client.post("/api/identities", json=_payload(daemon_id="d2", slug="reviewer-two"))
        client.post("/api/identities", json=_payload(slug="other", name="Other"))

        all_ids = client.get("/api/identities").json()["identities"]
        assert len(all_ids) == 3

        d1 = client.get("/api/identities", params={"daemon_id": "d1"}).json()["identities"]
        assert {i["daemon_id"] for i in d1} == {"d1"}
        assert len(d1) == 2

        # Deprecate one, then filter by status.
        client.post("/api/identities/did:agt:other/deprecate")
        active = client.get(
            "/api/identities", params={"status_filter": "active"}
        ).json()["identities"]
        assert len(active) == 2
        deprecated = client.get(
            "/api/identities", params={"status_filter": "deprecated"}
        ).json()["identities"]
        assert len(deprecated) == 1
        assert deprecated[0]["did"] == "did:agt:other"
    finally:
        dispose()


def test_get_by_did_found_and_missing(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload())
        got = client.get("/api/identities/did:agt:code-reviewer")
        assert got.status_code == 200
        assert got.json()["name"] == "Code Reviewer"

        missing = client.get("/api/identities/did:agt:ghost")
        assert missing.status_code == 404
    finally:
        dispose()


# ── Update ─────────────────────────────────────────────────────────


def test_update_partial_and_404(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload())
        resp = client.put(
            "/api/identities/did:agt:code-reviewer",
            json={"name": "Renamed", "add_capabilities": ["refactor"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["name"] == "Renamed"
        assert sorted(body["capabilities"]) == ["code-review", "refactor"]

        missing = client.put(
            "/api/identities/did:agt:ghost", json={"name": "x"}
        )
        assert missing.status_code == 404
    finally:
        dispose()


# ── Delete ─────────────────────────────────────────────────────────


def test_delete_and_404(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload())
        ident_dir = tmp_path / ".agt" / "identities" / "code-reviewer"
        assert (ident_dir / "AGENTS.md").exists()  # created on create

        ok = client.delete("/api/identities/did:agt:code-reviewer")
        assert ok.status_code == 204
        assert client.get("/api/identities/did:agt:code-reviewer").status_code == 404
        assert not ident_dir.exists()  # filesystem dir removed with identity

        missing = client.delete("/api/identities/did:agt:code-reviewer")
        assert missing.status_code == 404
    finally:
        dispose()


def test_delete_conflicts_when_task_references(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    # Rebuild engine access for the direct task insert.
    cfg_engine = make_engine(ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "identities.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    ))
    try:
        client.post("/api/identities", json=_payload())
        _add_task_ref(cfg_engine, "did:agt:code-reviewer", "t-ref-1")

        resp = client.delete("/api/identities/did:agt:code-reviewer")
        assert resp.status_code == 409
        assert "t-ref-1" in resp.json()["detail"]
        # Still exists.
        assert client.get("/api/identities/did:agt:code-reviewer").status_code == 200
    finally:
        asyncio.run(cfg_engine.dispose())
        dispose()


# ── Status transitions & sync-files ────────────────────────────────


def test_deprecate_and_revoke(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload(slug="aa", name="A"))
        client.post("/api/identities", json=_payload(slug="bb", name="B"))

        dep = client.post("/api/identities/did:agt:aa/deprecate")
        assert dep.status_code == 200
        assert dep.json()["status"] == "deprecated"

        rev = client.post("/api/identities/did:agt:bb/revoke")
        assert rev.status_code == 200
        assert rev.json()["status"] == "revoked"

        missing = client.post("/api/identities/did:agt:ghost/deprecate")
        assert missing.status_code == 404
    finally:
        dispose()


def test_sync_files_overwrites_manual_edits(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/identities", json=_payload())
        md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        md.write_text("MANUAL EDITS ONLY")

        resp = client.post("/api/identities/did:agt:code-reviewer/sync-files")
        assert resp.status_code == 200
        content = md.read_text()
        assert "MANUAL EDITS" not in content
        assert "Code Reviewer" in content

        missing = client.post("/api/identities/did:agt:ghost/sync-files")
        assert missing.status_code == 404
    finally:
        dispose()
