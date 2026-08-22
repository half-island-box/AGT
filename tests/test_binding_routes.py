"""Phase 4 Step 2 — binding + resolve route tests (FastAPI TestClient).

Verifies the HTTP surface added in Step 2:
- GET/POST /api/identities/{did}/bindings (+daemon filter, 201/409/404)
- DELETE /api/identities/{did}/bindings/{binding_id} (204/404)
- POST .../bindings/{binding_id}/default (200/404)
- POST /api/identities/resolve (200/400/404, no side-effects,
  not swallowed by the {did} route)

AGENT.md writes are redirected to tmp_path via monkeypatching
``routes.identities._root_dir`` (identity create writes the file).
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "binding-routes-test-secret"
DID = "did:agt:reviewer"


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "bindings.db"), secret=SECRET,
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


def _mk_identity(client: TestClient, tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)
    resp = client.post("/api/identities", json={
        "daemon_id": "local", "name": "Reviewer", "slug": "reviewer",
    })
    assert resp.status_code == 201


def _mk_binding(client: TestClient, **overrides) -> dict:
    body = {"daemon_id": "local", "provider": "claude", "priority": 0, "is_default": False}
    body.update(overrides)
    resp = client.post(f"/api/identities/{DID}/bindings", json=body)
    assert resp.status_code == 201
    return resp.json()


# ── binding CRUD ───────────────────────────────────────────────────


class TestBindingRoutes:
    def test_create_and_list(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            b1 = _mk_binding(client)
            b2 = _mk_binding(client, provider="codex", daemon_id="remote")

            assert b1["is_default"] is False
            assert b2["daemon_id"] == "remote"

            listed = client.get(f"/api/identities/{DID}/bindings").json()["bindings"]
            assert len(listed) == 2

            local = client.get(
                f"/api/identities/{DID}/bindings", params={"daemon_id": "local"}
            ).json()["bindings"]
            assert len(local) == 1
            assert local[0]["provider"] == "claude"
        finally:
            dispose()

    def test_duplicate_conflicts(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            _mk_binding(client)
            dup = client.post(f"/api/identities/{DID}/bindings", json={
                "daemon_id": "local", "provider": "claude",
            })
            assert dup.status_code == 409
        finally:
            dispose()

    def test_create_for_unknown_identity_404(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.post("/api/identities/did:agt:nope/bindings", json={
                "daemon_id": "local", "provider": "claude",
            })
            assert resp.status_code == 404
        finally:
            dispose()

    def test_delete_204_and_404(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            b = _mk_binding(client)

            ok = client.delete(f"/api/identities/{DID}/bindings/{b['id']}")
            assert ok.status_code == 204

            missing = client.delete(f"/api/identities/{DID}/bindings/{b['id']}")
            assert missing.status_code == 404
        finally:
            dispose()

    def test_set_default_switches_and_404(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            a = _mk_binding(client, provider="claude", is_default=True)
            b = _mk_binding(client, provider="codex")

            switched = client.post(f"/api/identities/{DID}/bindings/{b['id']}/default")
            assert switched.status_code == 200
            assert switched.json()["is_default"] is True

            listed = client.get(f"/api/identities/{DID}/bindings").json()["bindings"]
            flags = {x["provider"]: x["is_default"] for x in listed}
            assert flags == {"claude": False, "codex": True}

            missing = client.post(f"/api/identities/{DID}/bindings/99999/default")
            assert missing.status_code == 404
        finally:
            dispose()


# ── resolve ────────────────────────────────────────────────────────


class TestResolve:
    def test_resolve_success_and_no_side_effects(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            _mk_binding(client, provider="claude", is_default=True)

            resp = client.post("/api/identities/resolve", json={
                "identity_did": DID, "daemon_id": "local",
            })
            assert resp.status_code == 200
            body = resp.json()
            assert body == {"daemon_id": "local", "provider": "claude", "identity_did": DID}

            # No side-effects: bindings unchanged, identity unchanged.
            listed = client.get(f"/api/identities/{DID}/bindings").json()["bindings"]
            assert len(listed) == 1
        finally:
            dispose()

    def test_resolve_hint(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            _mk_binding(client, provider="claude")
            _mk_binding(client, provider="codex", daemon_id="remote")

            resp = client.post("/api/identities/resolve", json={
                "identity_did": DID, "runtime_hint": "codex", "daemon_id": "local",
            })
            assert resp.status_code == 200
            assert resp.json()["provider"] == "codex"
            assert resp.json()["daemon_id"] == "remote"
        finally:
            dispose()

    def test_resolve_missing_identity_did_400(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.post("/api/identities/resolve", json={"daemon_id": "local"})
            assert resp.status_code == 400
        finally:
            dispose()

    def test_resolve_not_found_404(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.post("/api/identities/resolve", json={
                "identity_did": "did:agt:nope", "daemon_id": "local",
            })
            assert resp.status_code == 404
            assert resp.json()["detail"]["type"] == "identity_not_found"
        finally:
            dispose()

    def test_resolve_not_active_400(self, tmp_path, monkeypatch) -> None:
        client, dispose = _build_app(tmp_path)
        try:
            _mk_identity(client, tmp_path, monkeypatch)
            client.post(f"/api/identities/{DID}/deprecate")

            resp = client.post("/api/identities/resolve", json={
                "identity_did": DID, "daemon_id": "local",
            })
            assert resp.status_code == 400
            assert resp.json()["detail"]["type"] == "identity_not_active"
        finally:
            dispose()

    def test_resolve_not_swallowed_by_did_route(self, tmp_path, monkeypatch) -> None:
        """POST /api/identities/resolve must hit the resolve handler, not
        be parsed as did='resolve' (static segment registered first)."""
        client, dispose = _build_app(tmp_path)
        try:
            resp = client.post("/api/identities/resolve", json={
                "identity_did": "did:agt:anyx", "daemon_id": "local",
            })
            # A did='resolve' route would 404 (no such identity); we expect
            # the resolve handler's own 404 (identity_not_found) or 200 —
            # but crucially the error must carry the err_type structure.
            assert resp.status_code in (200, 404)
            if resp.status_code == 404:
                assert resp.json()["detail"]["type"] == "identity_not_found"
        finally:
            dispose()
