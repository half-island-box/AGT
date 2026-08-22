"""Tests for the binding service layer (Phase 4 Step 2).

Follows the repo's async test convention (sync test functions + asyncio.run,
NullPool engine per test). Reuses the ``_DB`` harness shape from
test_identity_service.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agt.server.database import make_session_factory
from agt.server.models import Base
from agt.server.services.binding_service import (
    create_binding,
    delete_binding,
    list_bindings,
    set_default_binding,
)
from agt.server.services.identity_service import create_identity


class _DB:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/bind.db", poolclass=NullPool
        )
        self.factory = make_session_factory(self.engine)

    def run(self, coro_fn, *args, **kwargs):
        async def _inner():
            async with self.factory() as session:
                return await coro_fn(*args, **kwargs)
        return asyncio.run(_inner())

    def session(self):
        """Context manager yielding a session for raw ORM work."""
        return self.factory()

    def dispose(self) -> None:
        asyncio.run(self.engine.dispose())


@pytest.fixture
def db(tmp_path: Path) -> _DB:
    inst = _DB(tmp_path)

    async def _setup() -> None:
        async with inst.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    yield inst
    inst.dispose()


DID = "did:agt:reviewer"


async def _mk_identity(db: _DB, tmp_path: Path) -> None:
    async with db.session() as session:
        await create_identity(
            {"daemon_id": "local", "name": "Reviewer", "slug": "reviewer"},
            session,
            tmp_path,
        )


def _payload(**overrides) -> dict:
    base = {"daemon_id": "local", "provider": "claude", "priority": 0, "is_default": False}
    base.update(overrides)
    return base


# ── create ─────────────────────────────────────────────────────────


class TestCreate:
    def test_creates_binding_with_defaults(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        b = db.run(_create_via, db, DID, _payload())
        assert b["identity_did"] == DID
        assert b["daemon_id"] == "local"
        assert b["provider"] == "claude"
        assert b["priority"] == 0
        assert b["is_default"] is False
        assert isinstance(b["id"], int)

    def test_duplicate_triple_rejected(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_create_via, db, DID, _payload())
        with pytest.raises(ValueError, match="already exists"):
            db.run(_create_via, db, DID, _payload())

    def test_unknown_identity_rejected(self, db: _DB) -> None:
        with pytest.raises(ValueError, match="not found"):
            db.run(_create_via, db, "did:agt:nope", _payload())

    def test_priority_and_default_forwarded(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        b = db.run(_create_via, db, DID, _payload(priority=7, is_default=True))
        assert b["priority"] == 7
        assert b["is_default"] is True


# ── list / delete ──────────────────────────────────────────────────


class TestListDelete:
    def test_list_all_and_by_daemon(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_create_via, db, DID, _payload(provider="claude"))
        db.run(_create_via, db, DID, _payload(provider="codex", daemon_id="remote"))

        all_b = db.run(_list_via, db, DID)
        assert len(all_b) == 2
        local = db.run(_list_via, db, DID, daemon_id="local")
        assert len(local) == 1
        assert local[0]["provider"] == "claude"

    def test_delete_existing_and_missing(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        b = db.run(_create_via, db, DID, _payload())
        assert db.run(_delete_via, db, DID, b["id"]) is True
        assert db.run(_delete_via, db, DID, b["id"]) is False
        assert db.run(_list_via, db, DID) == []


# ── set_default atomicity ──────────────────────────────────────────


class TestSetDefault:
    def test_switch_unflags_old_default(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        a = db.run(_create_via, db, DID, _payload(provider="claude", is_default=True))
        b = db.run(_create_via, db, DID, _payload(provider="codex"))

        switched = db.run(_default_via, db, DID, b["id"])
        assert switched["is_default"] is True

        after = db.run(_list_via, db, DID)
        flags = {x["provider"]: x["is_default"] for x in after}
        assert flags == {"claude": False, "codex": True}

    def test_create_with_default_unflags_siblings(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_create_via, db, DID, _payload(provider="claude", is_default=True))
        db.run(_create_via, db, DID, _payload(provider="codex", is_default=True))

        flags = {x["provider"]: x["is_default"] for x in db.run(_list_via, db, DID)}
        assert flags == {"claude": False, "codex": True}

    def test_delete_default_leaves_none(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        b = db.run(_create_via, db, DID, _payload(provider="claude", is_default=True))
        db.run(_create_via, db, DID, _payload(provider="codex"))

        db.run(_delete_via, db, DID, b["id"])
        after = db.run(_list_via, db, DID)
        assert all(not x["is_default"] for x in after)  # no auto-promotion

    def test_default_unknown_binding_rejected(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        with pytest.raises(ValueError, match="not found"):
            db.run(_default_via, db, DID, 99999)


# ── harness helpers ────────────────────────────────────────────────


async def _create_via(db: _DB, did: str, payload: dict) -> dict:
    async with db.session() as session:
        return await create_binding(did, payload, session)


async def _list_via(db: _DB, did: str, daemon_id: str | None = None) -> list[dict]:
    async with db.session() as session:
        return await list_bindings(did, daemon_id=daemon_id, db=session)


async def _delete_via(db: _DB, did: str, binding_id: int) -> bool:
    async with db.session() as session:
        return await delete_binding(did, binding_id, session)


async def _default_via(db: _DB, did: str, binding_id: int) -> dict:
    async with db.session() as session:
        return await set_default_binding(did, binding_id, session)
