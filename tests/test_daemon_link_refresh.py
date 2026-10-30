"""Runtime daemon-link refresh (``POST /api/daemons/refresh``).

The ``~/.agt/daemons`` file used to be read once at server start, so a
target added while the Server was running needed a restart. These tests
cover the reconciliation and the re-dial kick:

- ``DaemonLinkPool.refresh`` adds / removes / replaces / re-dials links and
  leaves healthy ones alone (never disturb a working link).
- ``DaemonLink.kick`` cuts the reconnect backoff short.
- The HTTP endpoint maps the pool result and answers 503 without a pool.

Daemon ids in the file must be real UUIDs (``_parse_daemons_file`` ignores
anything else and derives the id from the URL), so the targets below carry
uuid5 ids and the assertions go through ``target.daemon_id``.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from starlette.testclient import TestClient

from agt.server.config import DaemonTarget, ServerConfig
from agt.server.daemon_link import DaemonLink
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base
from agt.server.services.daemon_link_pool import DaemonLinkPool

SECRET = "refresh-test-secret"
_ID_NAMESPACE = uuid.UUID("6f1d0f7e-0000-4000-8000-000000000000")


def _target(name: str, url: str | None = None) -> DaemonTarget:
    return DaemonTarget(
        daemon_id=str(uuid.uuid5(_ID_NAMESPACE, name)),
        name=name,
        url=url or f"ws://127.0.0.1:1/{name}",
    )


def _write_daemons(path: Path, targets: list[DaemonTarget]) -> None:
    lines = [f"{t.name} {t.url} id={t.daemon_id}" for t in targets]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class _FakeLink:
    """Stand-in with the surface ``DaemonLinkPool`` relies on."""

    def __init__(self, *, daemon_id: str, url: str, **_kwargs) -> None:
        self.daemon_id = daemon_id
        self.url = url
        self.connected = False
        self.kicks = 0
        self.stopped = False

    def kick(self) -> None:
        self.kicks += 1

    async def wait_connected(self, timeout: float) -> bool:
        return self.connected

    async def stop(self) -> None:
        self.stopped = True

    async def run(self, _shutdown: asyncio.Event) -> None:
        await asyncio.sleep(3600)


def _config(tmp_path: Path, targets: list[DaemonTarget]) -> ServerConfig:
    return ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
        daemons_path=str(tmp_path / "daemons"),
        daemons=list(targets),
    )


def _pool(
    tmp_path: Path, targets: list[DaemonTarget]
) -> tuple[DaemonLinkPool, dict, asyncio.Event]:
    cfg = _config(tmp_path, targets)
    defs = {t.daemon_id: t for t in targets}
    shutdown = asyncio.Event()
    pool = DaemonLinkPool(
        config=cfg,
        session_factory=None,
        daemon_defs=defs,
        shutdown=shutdown,
        link_factory=_FakeLink,
    )
    return pool, defs, shutdown


def _run(coro):
    return asyncio.run(coro)


# ── Reconciliation ──────────────────────────────────────────────────


def test_refresh_adds_new_target(tmp_path) -> None:
    a, b = _target("alpha"), _target("beta")
    _write_daemons(tmp_path / "daemons", [a, b])
    pool, defs, _shutdown = _pool(tmp_path, [a])

    async def _scenario() -> tuple[list[dict], set[str]]:
        await pool.start()
        pool.links[a.daemon_id].connected = True  # healthy link: leave alone
        report = await pool.refresh()
        live = set(pool.links)
        await pool.stop()
        return report, live

    report, live = _run(_scenario())

    by_id = {row["daemon_id"]: row for row in report}
    assert by_id[a.daemon_id]["action"] == "unchanged"
    assert by_id[b.daemon_id]["action"] == "added"
    assert live == {a.daemon_id, b.daemon_id}
    # The catalog overlay is mutated in place, not replaced: links and the
    # MCP tool hold this same dict.
    assert set(defs) == {a.daemon_id, b.daemon_id}


def test_refresh_removes_dropped_target(tmp_path) -> None:
    a, b = _target("alpha"), _target("beta")
    _write_daemons(tmp_path / "daemons", [a])
    pool, _defs, _shutdown = _pool(tmp_path, [a, b])

    async def _scenario() -> tuple[list[dict], bool, bool]:
        await pool.start()
        stopped_before = pool.links[b.daemon_id].stopped
        report = await pool.refresh()
        still_present = b.daemon_id in pool.links
        await pool.stop()
        return report, stopped_before, still_present

    report, stopped_before, still_present = _run(_scenario())

    assert [
        row["daemon_id"] for row in report if row["action"] == "removed"
    ] == [b.daemon_id]
    assert still_present is False
    assert stopped_before is False


def test_refresh_replaces_changed_url(tmp_path) -> None:
    a = _target("alpha")
    _write_daemons(tmp_path / "daemons", [_target("alpha", "ws://127.0.0.1:2/alpha")])
    pool, _defs, _shutdown = _pool(tmp_path, [a])

    async def _scenario() -> tuple[list[dict], bool]:
        await pool.start()
        old = pool.links[a.daemon_id]
        report = await pool.refresh()
        replaced = pool.links[a.daemon_id] is not old
        await pool.stop()
        return report, replaced

    report, replaced = _run(_scenario())
    assert [row["action"] for row in report] == ["replaced"]
    assert replaced


def test_refresh_leaves_healthy_link_untouched(tmp_path) -> None:
    a = _target("alpha")
    _write_daemons(tmp_path / "daemons", [a])
    pool, _defs, _shutdown = _pool(tmp_path, [a])

    async def _scenario() -> tuple[dict, _FakeLink]:
        await pool.start()
        link = pool.links[a.daemon_id]
        link.connected = True
        report = await pool.refresh()
        await pool.stop()
        return report[0], link

    row, link = _run(_scenario())
    assert row["action"] == "unchanged"
    assert link.kicks == 0


def test_refresh_redials_offline_link(tmp_path) -> None:
    a = _target("alpha")
    _write_daemons(tmp_path / "daemons", [a])
    pool, _defs, _shutdown = _pool(tmp_path, [a])

    async def _scenario() -> tuple[dict, _FakeLink]:
        await pool.start()
        link = pool.links[a.daemon_id]
        report = await pool.refresh()
        await pool.stop()
        return report[0], link

    row, link = _run(_scenario())
    assert row["action"] == "redialled"
    assert row["connected"] is False
    assert link.kicks == 1


def test_daemon_id_narrows_redial(tmp_path) -> None:
    a, b = _target("alpha"), _target("beta")
    _write_daemons(tmp_path / "daemons", [a, b])
    pool, _defs, _shutdown = _pool(tmp_path, [a, b])

    async def _scenario() -> tuple[dict, dict]:
        await pool.start()
        report = await pool.refresh(daemon_id=a.daemon_id)
        links = dict(pool.links)
        await pool.stop()
        return {row["daemon_id"]: row for row in report}, links

    by_id, links = _run(_scenario())
    assert by_id[a.daemon_id]["action"] == "redialled"
    assert by_id[b.daemon_id]["action"] == "unchanged"
    assert links[a.daemon_id].kicks == 1
    assert links[b.daemon_id].kicks == 0


def test_missing_file_keeps_registry(tmp_path) -> None:
    """A deleted/typo'd daemons file must not unplug live links."""
    a = _target("alpha")
    pool, _defs, _shutdown = _pool(tmp_path, [a])

    async def _scenario() -> tuple[list[dict], bool]:
        await pool.start()
        report = await pool.refresh()
        still_present = a.daemon_id in pool.links
        await pool.stop()
        return report, still_present

    report, still_present = _run(_scenario())
    assert [row["daemon_id"] for row in report] == [a.daemon_id]
    assert still_present


def test_dead_run_loop_is_dropped(tmp_path) -> None:
    """A run-loop that dies is removed so a later refresh rebuilds it."""
    class _DyingLink(_FakeLink):
        async def run(self, _shutdown: asyncio.Event) -> None:
            raise RuntimeError("boom")

    a = _target("alpha")
    _write_daemons(tmp_path / "daemons", [a])
    cfg = _config(tmp_path, [a])
    defs = {a.daemon_id: a}
    shutdown = asyncio.Event()
    pool = DaemonLinkPool(
        config=cfg,
        session_factory=None,
        daemon_defs=defs,
        shutdown=shutdown,
        link_factory=_DyingLink,
    )

    async def _scenario() -> None:
        await pool.start()
        for _ in range(20):
            if a.daemon_id not in pool.links:
                break
            await asyncio.sleep(0.01)
        assert a.daemon_id not in pool.links
        assert a.daemon_id not in pool._tasks
        # Rebuilding works.
        await pool.refresh()
        assert a.daemon_id in pool.links
        await pool.stop()

    _run(_scenario())


# ── kick() cuts the backoff short ───────────────────────────────────


def test_kick_redials_without_waiting_out_backoff() -> None:
    """The run loop retries immediately after ``kick`` instead of ~1s later."""
    link = DaemonLink(
        daemon_id="d", url="ws://127.0.0.1:1", secret="s", session_factory=None,
    )
    attempts = 0

    async def _failing_connect() -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("refused")

    link._connect_and_serve = _failing_connect  # type: ignore[method-assign]

    async def _scenario() -> None:
        shutdown = asyncio.Event()
        task = asyncio.create_task(link.run(shutdown))
        try:
            await asyncio.sleep(0.05)
            assert attempts == 1  # first attempt failed; now in backoff
            link.kick()
            await asyncio.sleep(0.05)
            assert attempts == 2  # woke up and retried at once
        finally:
            shutdown.set()
            link.kick()  # cut the current sleep so shutdown is noticed now
            await asyncio.wait_for(task, timeout=2.0)

    _run(_scenario())


# ── HTTP endpoint ───────────────────────────────────────────────────


def _build_app(tmp_path: Path, *, daemons: list[DaemonTarget] | None = None):
    cfg = _config(tmp_path, daemons or [])
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


def test_refresh_endpoint_returns_rows(tmp_path) -> None:
    a = _target("alpha")
    _write_daemons(tmp_path / "daemons", [a])
    client, dispose = _build_app(tmp_path, daemons=[a])
    try:
        with client:
            resp = client.post("/api/daemons/refresh")
            assert resp.status_code == 200
            body = resp.json()
            assert body["daemons_path"] == str(tmp_path / "daemons")
            assert [row["daemon_id"] for row in body["daemons"]] == [a.daemon_id]
    finally:
        dispose()


def test_refresh_endpoint_without_pool_503(tmp_path) -> None:
    client, dispose = _build_app(tmp_path)
    try:
        client.app.state.daemon_link_pool = None
        resp = client.post("/api/daemons/refresh")
        assert resp.status_code == 503
        assert resp.json()["detail"]["type"] == "daemon_pool_unavailable"
    finally:
        dispose()
