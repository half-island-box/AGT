"""Phase 4 Step 4 — submit_task with identity_did.

Verifies the ``select_runtime`` + binding check integration in
``POST /api/tasks/submit``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base, IdentityModel, RuntimeModel
from agt.server.services.binding_service import create_binding
from agt.server.services.identity_service import create_identity


SECRET = "submit-identity-test-secret"


def _build_app(tmp_path: Path) -> tuple[TestClient, callable, async_sessionmaker]:
    """Build TestClient + expose the session factory so tests can seed
    via the SAME engine/client (avoids separate-engine file-state races)."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "server.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
            await conn.run_sync(
                migrate_identities_and_sessions, tmp_path
            )

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return client, _dispose, factory


async def _seed_identity_with_root(factory, *, slug: str, root_dir: Path) -> None:
    """Create an identity via the service (DID derived from slug)."""
    async with factory() as session:
        await create_identity(
            {"daemon_id": "any", "name": slug, "slug": slug},
            session, root_dir,
        )
        await session.commit()


async def _seed_binding(factory, *, did: str, daemon_id: str, provider: str,
                        is_default: bool = False) -> None:
    async with factory() as session:
        await create_binding(
            did,
            {"daemon_id": daemon_id, "provider": provider, "priority": 0,
             "is_default": is_default},
            session,
        )
        await session.commit()


async def _seed_runtime(factory, *, daemon_id: str, provider: str,
                       last_seen: str | None = None) -> None:
    if last_seen is None:
        last_seen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    async with factory() as session:
        session.add(RuntimeModel(
            daemon_id=daemon_id, provider=provider,
            name=f"{provider} ({daemon_id})", version="1.0",
            status="online", device_name="dev",
            last_seen=last_seen,
        ))
        await session.commit()


async def _set_status(factory, did: str, status: str) -> None:
    async with factory() as session:
        ident = await session.get(IdentityModel, did)
        ident.status = status
        await session.commit()


# ── happy paths ───────────────────────────────────────────────────


class TestIdentityResolvesRuntime:
    def test_default_binding_picked_when_no_provider(self, tmp_path) -> None:
        async def _seed() -> None:
            await _seed_identity_with_root(
                factory, slug="reviewer",
                root_dir=tmp_path,
            )
            await _seed_binding(factory, did="did:agt:reviewer",
                                daemon_id="d1", provider="claude",
                                is_default=True)
            await _seed_runtime(factory, daemon_id="d1", provider="claude")

        client, dispose, factory = _build_app(tmp_path)
        try:
            asyncio.run(_seed())

            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:reviewer",
            })
            assert r.status_code == 201, r.json()
            tid = r.json()["task_id"]

            status = client.get(f"/api/tasks/{tid}/status").json()
            assert status["identity_did"] == "did:agt:reviewer"
            assert status["provider"] == "claude"
            assert status["target_daemon_id"] == "d1"
        finally:
            dispose()

    def test_provider_used_as_runtime_hint(self, tmp_path) -> None:
        """Identity has both claude/codex bindings; provider=codex picks codex."""
        async def _seed() -> None:
            # One identity with two bindings (different providers).
            await _seed_identity_with_root(
                factory, slug="polyglot",
                root_dir=tmp_path,
            )
            for prov in ("claude", "codex"):
                await _seed_binding(
                    factory, did="did:agt:polyglot",
                    daemon_id="d1", provider=prov,
                )
                await _seed_runtime(factory, daemon_id="d1", provider=prov)

        client, dispose, factory = _build_app(tmp_path)
        try:
            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "codex",
                "identity_did": "did:agt:polyglot",
            })
            assert r.status_code == 201
            status = client.get(f"/api/tasks/{r.json()['task_id']}/status").json()
            assert status["provider"] == "codex"
        finally:
            dispose()


# ── error mapping ─────────────────────────────────────────────────


class TestErrorMapping:
    def test_identity_not_found_404(self, tmp_path) -> None:
        client, dispose, _factory = _build_app(tmp_path)
        try:
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:ghost",
            })
            assert r.status_code == 404
            assert r.json()["detail"]["type"] == "identity_not_found"
        finally:
            dispose()

    def test_identity_not_active_400(self, tmp_path) -> None:
        client, dispose, factory = _build_app(tmp_path)
        try:
            async def _seed() -> None:
                await _seed_identity_with_root(
                    factory, slug="old",
                    root_dir=tmp_path,
                )
                await _seed_binding(factory, did="did:agt:old",
                                    daemon_id="d1", provider="claude")
                await _set_status(factory, "did:agt:old", "deprecated")

            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:old",
            })
            assert r.status_code == 400
            detail = r.json()["detail"]
            assert detail["type"] == "identity_not_active"
            assert detail["status"] == "deprecated"
        finally:
            dispose()

    def test_no_bindings_allowed_with_live_runtime(self, tmp_path) -> None:
        client, dispose, factory = _build_app(tmp_path)
        try:
            async def _seed() -> None:
                await _seed_identity_with_root(
                    factory, slug="lonely",
                    root_dir=tmp_path,
                )
                await _seed_runtime(factory, daemon_id="d1", provider="claude")

            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:lonely",
            })
            assert r.status_code == 201
            status = client.get(f"/api/tasks/{r.json()['task_id']}/status").json()
            assert status["identity_did"] == "did:agt:lonely"
            assert status["provider"] == "claude"
            assert status["target_daemon_id"] == "d1"
        finally:
            dispose()

    def test_unbound_provider_allowed_with_live_runtime(self, tmp_path) -> None:
        client, dispose, factory = _build_app(tmp_path)
        try:
            async def _seed() -> None:
                await _seed_identity_with_root(
                    factory, slug="only-claude",
                    root_dir=tmp_path,
                )
                await _seed_binding(
                    factory, did="did:agt:only-claude",
                    daemon_id="d1", provider="claude",
                )
                await _seed_runtime(factory, daemon_id="d1", provider="claude")
                await _seed_runtime(factory, daemon_id="d2", provider="cursor")

            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "cursor",
                "identity_did": "did:agt:only-claude",
            })
            assert r.status_code == 201
            status = client.get(f"/api/tasks/{r.json()['task_id']}/status").json()
            assert status["provider"] == "cursor"
            assert status["target_daemon_id"] == "d2"
            assert status["identity_did"] == "did:agt:only-claude"
        finally:
            dispose()

    def test_no_binding_or_runtime_400(self, tmp_path) -> None:
        client, dispose, factory = _build_app(tmp_path)
        try:
            async def _seed() -> None:
                await _seed_identity_with_root(
                    factory, slug="lonely",
                    root_dir=tmp_path,
                )

            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:lonely",
            })
            assert r.status_code == 400
            assert r.json()["detail"]["type"] == "no_live_runtime"
        finally:
            dispose()


# ── legacy path ───────────────────────────────────────────────────


class TestLegacyPath:
    def test_no_identity_did_uses_provider_verbatim(self, tmp_path) -> None:
        client, dispose, _ = _build_app(tmp_path)
        try:
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
            })
            assert r.status_code == 201
            status = client.get(f"/api/tasks/{r.json()['task_id']}/status").json()
            assert status["identity_did"] is None
            assert status["provider"] == "claude"
        finally:
            dispose()

    def test_no_identity_did_still_requires_live_runtime(self, tmp_path) -> None:
        client, dispose, _ = _build_app(tmp_path)
        try:
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "target_daemon_id": "ghost-daemon",
            })
            assert r.status_code == 400
            assert "live runtime" in r.json()["detail"]
        finally:
            dispose()


# ── selection result + live-runtime check ─────────────────────────


class TestLiveRuntimeStillChecked:
    def test_identity_picks_daemon_but_runtime_stale(self, tmp_path) -> None:
        client, dispose, factory = _build_app(tmp_path)
        try:
            async def _seed() -> None:
                await _seed_identity_with_root(
                factory, slug="stale",
                root_dir=tmp_path,
            )
                await _seed_binding(
                    factory, did="did:agt:stale",
                    daemon_id="d1", provider="claude",
                )
                # Register runtime as STALE (backdated last_seen)
                stale_ts = datetime(2020, 1, 1, tzinfo=timezone.utc).strftime(
                    "%Y-%m-%d %H:%M:%S"
                )
                await _seed_runtime(factory, daemon_id="d1",
                                    provider="claude", last_seen=stale_ts)

            asyncio.run(_seed())
            r = client.post("/api/tasks/submit", json={
                "prompt": "hi", "provider": "claude",
                "identity_did": "did:agt:stale",
            })
            assert r.status_code == 400
            assert "live runtime" in r.json()["detail"]
        finally:
            dispose()
