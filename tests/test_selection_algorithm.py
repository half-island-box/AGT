"""Tests for the selection algorithm (Phase 4 Step 2, spec: selection-algorithm).

Covers the four-step decision flow from phase4-design.md §5.1:
identity validation → load bindings → runtime_hint filter → default/priority.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agt.server.database import make_session_factory
from agt.server.models import Base
from agt.server.services.binding_service import create_binding
from agt.server.services.identity_service import create_identity
from agt.server.services.selection_service import (
    SelectionError,
    select_runtime,
)


class _DB:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/sel.db", poolclass=NullPool
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


async def _mk_identity(db: _DB, tmp_path: Path, status: str | None = None) -> None:
    async with db.session() as session:
        payload = {"daemon_id": "local", "name": "Reviewer", "slug": "reviewer"}
        await create_identity(payload, session, tmp_path)
        if status is not None:
            from agt.server.models import IdentityModel
            row = await session.get(IdentityModel, DID)
            row.status = status
            await session.commit()


async def _bind(db: _DB, **kw) -> None:
    payload = {"daemon_id": "local", "provider": "claude", "priority": 0, "is_default": False}
    payload.update(kw)
    async with db.session() as session:
        await create_binding(DID, payload, session)


async def _select(db: _DB, hint: str | None):
    async with db.session() as session:
        return await select_runtime(DID, hint, "local", session)


async def _seed_runtime(
    db: _DB, daemon_id: str, provider: str, *, stale: bool = False
) -> None:
    """Insert a runtime row for (daemon_id, provider).

    ``stale=True`` backdates ``last_seen`` beyond the liveness TTL so
    the row looks like a daemon that stopped heartbeating.
    """
    from datetime import datetime, timedelta, timezone

    from agt.server.models import RuntimeModel
    from agt.server.services.runtime_service import TS_FMT

    ts = datetime.now(timezone.utc).replace(tzinfo=None)
    if stale:
        ts -= timedelta(seconds=3600)
    async with db.session() as session:
        session.add(
            RuntimeModel(
                daemon_id=daemon_id, provider=provider, name=provider,
                version="1.0", status="online", device_name="dev",
                last_seen=ts.strftime(TS_FMT),
            )
        )
        await session.commit()


# ── happy paths ────────────────────────────────────────────────────


class TestHappyPaths:
    def test_hint_hit(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude")
        db.run(_bind, db, provider="codex", daemon_id="remote")

        result = db.run(_select, db, "codex")
        assert result.daemon_id == "remote"
        assert result.provider == "codex"
        assert result.identity_did == DID

    def test_default_beats_priority(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude", is_default=True)
        db.run(_bind, db, provider="codex", priority=100)

        result = db.run(_select, db, None)
        assert result.provider == "claude"

    def test_priority_fallback_without_default(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude", priority=5)
        db.run(_bind, db, provider="codex", priority=10)

        result = db.run(_select, db, None)
        assert result.provider == "codex"

    def test_tie_break_stable_by_id(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude", priority=10)   # id 1
        db.run(_bind, db, provider="codex", priority=10)    # id 2

        # max() with a key returns the FIRST max in iteration order (id asc).
        first = db.run(_select, db, None)
        second = db.run(_select, db, None)
        assert first.provider == second.provider == "claude"


# ── liveness-aware preference ───────────────────────────────────────


class TestLivenessPreference:
    """A stale binding must never shadow a healthy one just because it
    was created earlier (the foot-gun behind the "daemon … has no live
    runtime" 400)."""

    def test_hint_prefers_live_over_earlier_dead(self, db: _DB, tmp_path) -> None:
        # The user's exact bug: older binding on a dead daemon, newer
        # binding on a live daemon, same provider.
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="codex", daemon_id="dead-daemon")    # id 1
        db.run(_bind, db, provider="codex", daemon_id="live-daemon")   # id 2
        db.run(_seed_runtime, db, "live-daemon", "codex")

        result = db.run(_select, db, "codex")
        assert result.daemon_id == "live-daemon"

    def test_hint_treats_stale_row_as_dead(self, db: _DB, tmp_path) -> None:
        # A runtime row older than the liveness TTL is no proof of life.
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="codex", daemon_id="gone-daemon")   # id 1
        db.run(_bind, db, provider="codex", daemon_id="live-daemon")   # id 2
        db.run(_seed_runtime, db, "gone-daemon", "codex", stale=True)
        db.run(_seed_runtime, db, "live-daemon", "codex")

        result = db.run(_select, db, "codex")
        assert result.daemon_id == "live-daemon"

    def test_hint_default_flag_wins_among_live(self, db: _DB, tmp_path) -> None:
        # Both live; the is_default binding wins even with the larger id
        # (previously the hint path always took matching[0]).
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="codex", daemon_id="live-a")        # id 1
        db.run(_bind, db, provider="codex", daemon_id="live-b", is_default=True)
        db.run(_seed_runtime, db, "live-a", "codex")
        db.run(_seed_runtime, db, "live-b", "codex")

        result = db.run(_select, db, "codex")
        assert result.daemon_id == "live-b"

    def test_hint_all_dead_falls_back_to_default(self, db: _DB, tmp_path) -> None:
        # Nothing live → fall back to the pre-liveness ordering (default
        # flag first). The submit-time live-runtime check then rejects.
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="codex", daemon_id="dead-a")        # id 1
        db.run(_bind, db, provider="codex", daemon_id="dead-b", is_default=True)

        result = db.run(_select, db, "codex")
        assert result.daemon_id == "dead-b"

    def test_no_hint_prefers_live(self, db: _DB, tmp_path) -> None:
        # Without a hint: the dead daemon's binding has higher priority
        # and a smaller id, but liveness still wins.
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude", daemon_id="dead-daemon", priority=100)
        db.run(_bind, db, provider="codex", daemon_id="live-daemon", priority=0)
        db.run(_seed_runtime, db, "live-daemon", "codex")

        result = db.run(_select, db, None)
        assert result.daemon_id == "live-daemon"
        assert result.provider == "codex"


# ── error paths ────────────────────────────────────────────────────


class TestErrors:
    def test_identity_not_found(self, db: _DB) -> None:
        with pytest.raises(SelectionError) as exc_info:
            db.run(_select, db, None)
        assert exc_info.value.err_type == "identity_not_found"
        assert exc_info.value.context["did"] == DID

    def test_identity_not_active(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path, status="deprecated")
        with pytest.raises(SelectionError) as exc_info:
            db.run(_select, db, None)
        assert exc_info.value.err_type == "identity_not_active"
        assert exc_info.value.context["status"] == "deprecated"

    def test_revoked_also_not_active(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path, status="revoked")
        with pytest.raises(SelectionError) as exc_info:
            db.run(_select, db, None)
        assert exc_info.value.err_type == "identity_not_active"

    def test_no_bindings(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        with pytest.raises(SelectionError) as exc_info:
            db.run(_select, db, None)
        assert exc_info.value.err_type == "no_bindings"
        assert "agt identity bind" in str(exc_info.value)

    def test_no_binding_for_runtime(self, db: _DB, tmp_path: Path) -> None:
        db.run(_mk_identity, db, tmp_path)
        db.run(_bind, db, provider="claude")
        db.run(_bind, db, provider="codex")

        with pytest.raises(SelectionError) as exc_info:
            db.run(_select, db, "cursor")
        err = exc_info.value
        assert err.err_type == "no_binding_for_runtime"
        assert err.context["runtime"] == "cursor"
        assert err.context["available"] == ["claude", "codex"]

    def test_selection_error_is_value_error(self, db: _DB) -> None:
        """Routes translate ValueError; SelectionError must be catchable as one."""
        with pytest.raises(ValueError):
            db.run(_select, db, None)
