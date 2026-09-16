"""Tests for the daemon aggregation service (Phase 4, daemon-registry-view).

Verifies grouping, latest-heartbeat-wins, online derivation via is_stale,
and that offline daemons stay visible.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agt.server.database import make_session_factory
from agt.server.models import Base, RuntimeModel
from agt.server.config import DaemonTarget
from agt.server.services.daemon_service import get_daemon, list_daemons
from agt.server.services.runtime_service import TS_FMT


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stamp(age_seconds: float) -> str:
    return (_utcnow() - timedelta(seconds=age_seconds)).strftime(TS_FMT)


class _DB:
    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/daemon.db", poolclass=NullPool
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


def _row(daemon_id: str, provider: str, device: str = "dev", last_seen: str = "", version: str = "1.0") -> RuntimeModel:
    return RuntimeModel(
        daemon_id=daemon_id,
        provider=provider,
        name=f"{provider} ({daemon_id})",
        version=version,
        status="online",
        device_name=device,
        last_seen=last_seen,
    )


async def _insert(db: _DB, *rows: RuntimeModel) -> None:
    async with db.session() as session:
        session.add_all(rows)
        await session.commit()


async def _list(db: _DB):
    async with db.session() as session:
        return await list_daemons(session)


async def _get(db: _DB, did: str):
    async with db.session() as session:
        return await get_daemon(did, session)


class TestListAggregation:
    def test_multi_provider_daemon_aggregates(self, db: _DB) -> None:
        db.run(_insert, db,
            _row("d1", "claude", last_seen=_stamp(1)),
            _row("d1", "codex", last_seen=_stamp(2)),
            _row("d2", "claude", last_seen=_stamp(5)),
        )
        daemons = db.run(_list, db)
        by_id = {d["daemon_id"]: d for d in daemons}
        assert set(by_id) == {"d1", "d2"}

        d1 = by_id["d1"]
        assert d1["runtime_count"] == 2
        assert {p["provider"] for p in d1["providers"]} == {"claude", "codex"}

    def test_latest_heartbeat_wins_for_fields(self, db: _DB) -> None:
        db.run(_insert, db,
            _row("d1", "claude", device="old-device", last_seen=_stamp(100), version="1.0"),
            _row("d1", "codex", device="new-device", last_seen=_stamp(1), version="2.0"),
        )
        d1 = db.run(_get, db, "d1")
        assert d1["device_name"] == "new-device"
        assert d1["last_seen"] != ""

    def test_online_derived_from_latest(self, db: _DB) -> None:
        # claude row stale, codex row fresh → online (latest is fresh).
        db.run(_insert, db,
            _row("d1", "claude", last_seen=_stamp(10_000)),
            _row("d1", "codex", last_seen=_stamp(1)),
        )
        assert db.run(_get, db, "d1")["online"] is True

    def test_stale_daemon_still_listed_offline(self, db: _DB) -> None:
        db.run(_insert, db, _row("d-old", "codex", last_seen=_stamp(60_000)))
        daemon = db.run(_get, db, "d-old")
        assert daemon is not None
        assert daemon["online"] is False
        assert daemon["last_seen"] != ""  # heartbeat visible even offline

    def test_empty_last_seen_offline(self, db: _DB) -> None:
        db.run(_insert, db, _row("d-blank", "claude", last_seen=""))
        assert db.run(_get, db, "d-blank")["online"] is False

    def test_no_daemons_returns_empty(self, db: _DB) -> None:
        assert db.run(_list, db) == []


class TestGet:
    def test_get_known_daemon(self, db: _DB) -> None:
        db.run(_insert, db, _row("d1", "claude", last_seen=_stamp(1)))
        daemon = db.run(_get, db, "d1")
        assert daemon is not None
        assert daemon["daemon_id"] == "d1"
        assert len(daemon["providers"]) == 1

    def test_get_unknown_returns_none(self, db: _DB) -> None:
        assert db.run(_get, db, "did-not-exist") is None


class TestStaticOverlay:
    """Phase A: `~/.agt/daemons` url/capabilities overlay merge."""

    def _defs(self, **kw) -> dict[str, DaemonTarget]:
        return {
            "d1": DaemonTarget(
                daemon_id="d1",
                name=kw.get("name", "cloud"),
                url=kw.get("url", "ws://h:8422"),
                capabilities=kw.get("capabilities", ("containers", "logs")),
            )
        }

    def test_overlay_merges_url_and_capabilities(self, db: _DB) -> None:
        db.run(_insert, db, _row("d1", "claude", last_seen=_stamp(1)))

        async def _run():
            async with db.session() as session:
                return await get_daemon("d1", session, daemon_defs=self._defs())

        d = asyncio.run(_run())
        assert d["url"] == "ws://h:8422"
        assert d["capabilities"] == ["containers", "logs"]

    def test_overlay_fills_missing_name(self, db: _DB) -> None:
        db.run(_insert, db, _row("d1", "claude", last_seen=_stamp(1)))

        async def _run():
            async with db.session() as session:
                return await get_daemon("d1", session, daemon_defs=self._defs())

        d = asyncio.run(_run())
        assert d["daemon_name"] == "cloud"

    def test_absent_target_omits_overlay_keys(self, db: _DB) -> None:
        db.run(_insert, db, _row("d1", "claude", last_seen=_stamp(1)))

        async def _run():
            async with db.session() as session:
                return await list_daemons(session, daemon_defs={})

        result = asyncio.run(_run())
        assert result[0]["daemon_id"] == "d1"
        assert "url" not in result[0]
        assert "capabilities" not in result[0]


class _Link:
    """Minimal ``DaemonLink`` stub for the ``connected`` online signal."""

    def __init__(self, connected: bool) -> None:
        self._connected = connected

    @property
    def connected(self) -> bool:
        return self._connected


class TestConfigBaseList:
    """Configured daemons are the base list, even before any heartbeat."""

    def _target(self, **kw) -> DaemonTarget:
        return DaemonTarget(
            daemon_id=kw.get("daemon_id", "d-cfg"),
            name=kw.get("name", "cloud"),
            url=kw.get("url", "ws://h:8422"),
            capabilities=kw.get("capabilities", ("containers", "logs")),
        )

    def test_config_only_daemon_listed_offline(self, db: _DB) -> None:
        async def _run():
            async with db.session() as session:
                return await list_daemons(session, daemon_defs={"d-cfg": self._target()})

        result = asyncio.run(_run())
        assert len(result) == 1
        d = result[0]
        assert d["daemon_id"] == "d-cfg"
        assert d["daemon_name"] == "cloud"
        assert d["runtime_count"] == 0
        assert d["providers"] == []
        assert d["online"] is False
        assert d["last_seen"] is None
        assert d["device_name"] is None
        assert d["url"] == "ws://h:8422"
        assert d["capabilities"] == ["containers", "logs"]

    def test_config_daemon_merged_with_runtime(self, db: _DB) -> None:
        db.run(_insert, db, _row("d-cfg", "claude", device="dev", last_seen=_stamp(1)))

        async def _run():
            async with db.session() as session:
                return await list_daemons(session, daemon_defs={"d-cfg": self._target()})

        d = asyncio.run(_run())[0]
        assert d["runtime_count"] == 1
        assert d["device_name"] == "dev"
        assert d["online"] is True
        assert d["url"] == "ws://h:8422"  # static overlay still merged

    def test_config_first_then_runtime_only(self, db: _DB) -> None:
        db.run(_insert, db,
            _row("local-1", "claude", last_seen=_stamp(1)),
            _row("d-cfg", "codex", last_seen=_stamp(1)),
        )

        async def _run():
            async with db.session() as session:
                return await list_daemons(session, daemon_defs={"d-cfg": self._target()})

        result = asyncio.run(_run())
        assert [d["daemon_id"] for d in result] == ["d-cfg", "local-1"]

    def test_link_connected_marks_online_without_heartbeat(self, db: _DB) -> None:
        async def _run():
            async with db.session() as session:
                return await list_daemons(
                    session,
                    daemon_defs={"d-cfg": self._target()},
                    daemon_links={"d-cfg": _Link(True)},
                )

        assert asyncio.run(_run())[0]["online"] is True

    def test_get_config_only_daemon_not_none(self, db: _DB) -> None:
        async def _run():
            async with db.session() as session:
                return await get_daemon("d-cfg", session, daemon_defs={"d-cfg": self._target()})

        d = asyncio.run(_run())
        assert d is not None
        assert d["online"] is False
        assert d["runtime_count"] == 0
