"""Runtime liveness — TTL + startup sweep + read-time filtering.

Verifies the changes that turn ``runtimes`` from a permanent accumulator
into a TTL-bounded live registry:

- ``is_stale`` predicate: fresh / old / empty / unparseable boundaries.
- ``prune_stale_runtimes``: only removes rows past the TTL, leaves fresh
  rows alone.
- ``list_runtimes`` (HTTP path): filters stale rows out of the response.
- ``submit_task`` (HTTP path): rejects ``--daemon`` targeting a daemon
  whose only matching row is stale, so a user can't pin a task to a
  daemon that can never claim it.
- Server ``lifespan``: startup sweep drops stale rows before the first
  request lands.
- ``DaemonWsServer.on_connect`` fires after ``current_link`` is set,
  swallows hook exceptions, and pushes the snapshot to the Server on a
  real reverse-dial handshake.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from starlette.testclient import TestClient

from agt.comm.errors import WSRPCUnavailable
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, RuntimeModel
from agt.server.services.runtime_service import (
    RUNTIME_STALE_SECONDS,
    TS_FMT,
    is_stale,
    prune_stale_runtimes,
    upsert_daemon_runtimes,
)


SECRET = "runtime-liveness-test-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _row(daemon_id: str, provider: str, *, last_seen: str) -> RuntimeModel:
    return RuntimeModel(
        daemon_id=daemon_id,
        provider=provider,
        name=f"{provider} ({daemon_id})",
        version="1.0.0",
        status="online",
        device_name="dev",
        last_seen=last_seen,
    )


# ── is_stale predicate ────────────────────────────────────────────


def test_is_stale_fresh_row_is_false() -> None:
    now = _utcnow_naive()
    fresh = now.strftime(TS_FMT)
    assert is_stale(fresh, now=now) is False


def test_is_stale_just_inside_ttl_is_false() -> None:
    """A row stamped 1 second inside the boundary must be fresh.

    We use ``ttl - 1`` rather than ``ttl`` exactly because
    ``last_seen`` is stored at second resolution — testing at exactly
    ``ttl`` would race against the truncation to seconds and yield
    ``(now - ts) == ttl + 0.5`` (≈ 0.5s of microseconds rounding),
    which the predicate correctly judges stale."""
    now = _utcnow_naive()
    ts = now - timedelta(seconds=RUNTIME_STALE_SECONDS - 1)
    assert is_stale(ts.strftime(TS_FMT), now=now) is False


def test_is_stale_just_past_ttl_is_true() -> None:
    now = _utcnow_naive()
    ts = now - timedelta(seconds=RUNTIME_STALE_SECONDS + 1)
    assert is_stale(ts.strftime(TS_FMT), now=now) is True


def test_is_stale_empty_string_is_true() -> None:
    """Empty ``last_seen`` means we have no proof of liveness."""
    assert is_stale("", now=_utcnow_naive()) is True


def test_is_stale_unparseable_string_is_true() -> None:
    """Garbage in ``last_seen`` → stale; we'd rather drop a row than keep
    a row whose liveness we cannot prove."""
    assert is_stale("not-a-timestamp", now=_utcnow_naive()) is True


# ── prune_stale_runtimes ──────────────────────────────────────────


def test_prune_removes_only_stale_rows() -> None:
    """Mixed registry: fresh rows stay, stale rows go, by id."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_liveness_prune.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
    )
    engine = make_engine(cfg)

    async def _run() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            await db.commit()
        now = _utcnow_naive()
        fresh = now.strftime(TS_FMT)
        old = (now - timedelta(seconds=RUNTIME_STALE_SECONDS + 30)).strftime(TS_FMT)
        very_old = (now - timedelta(seconds=RUNTIME_STALE_SECONDS * 10)).strftime(TS_FMT)
        async with session_scope(factory) as db:
            db.add_all([
                _row("d1", "claude", last_seen=fresh),       # keep
                _row("d1", "codex", last_seen=fresh),        # keep
                _row("d2", "claude", last_seen=old),         # drop
                _row("d3", "codex", last_seen=very_old),     # drop
            ])
            await db.commit()
        async with session_scope(factory) as db:
            removed = await prune_stale_runtimes(db)
        assert removed == 2
        async with session_scope(factory) as db:
            rows = (await db.execute(select(RuntimeModel).order_by(RuntimeModel.daemon_id))).scalars().all()
        assert {(r.daemon_id, r.provider) for r in rows} == {
            ("d1", "claude"), ("d1", "codex"),
        }

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_prune_does_not_touch_fresh_rows() -> None:
    """``last_seen`` is updated by upsert; never let the sweep regress."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_liveness_prune_keep.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
    )
    engine = make_engine(cfg)

    async def _run() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            await db.commit()
        # Run 1: seed fresh via upsert; expect prune to remove 0.
        async with session_scope(factory) as db:
            await upsert_daemon_runtimes(
                db, daemon_id="d-live", runtimes=[
                    {"name": "Claude", "type": "claude", "version": "1",
                     "status": "online", "device_name": "dev"},
                ],
            )
        async with session_scope(factory) as db:
            removed = await prune_stale_runtimes(db)
        assert removed == 0
        async with session_scope(factory) as db:
            rows = (await db.execute(select(RuntimeModel))).scalars().all()
        assert len(rows) == 1

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── list_runtimes (HTTP path) ─────────────────────────────────────


def _build_app_with_runtime_rows(
    rows: list[tuple[str, str, str]],  # (daemon_id, provider, last_seen)
) -> tuple[TestClient, callable]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_liveness_list.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
    )

    async def _setup() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            for daemon_id, provider, last_seen in rows:
                db.add(_row(daemon_id, provider, last_seen=last_seen))
            await db.commit()
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    async def _dispose() -> None:
        await cfg._engine_ref.dispose()  # type: ignore[attr-defined]

    def _dispose_sync() -> None:
        asyncio.run(_dispose())

    return client, _dispose_sync


def test_list_runtimes_filters_stale_rows() -> None:
    now = _utcnow_naive()
    fresh = now.strftime(TS_FMT)
    stale = (now - timedelta(seconds=RUNTIME_STALE_SECONDS + 10)).strftime(TS_FMT)
    client, dispose = _build_app_with_runtime_rows([
        ("d-live", "claude", fresh),
        ("d-dead", "claude", stale),
        ("d-dead", "codex", stale),
    ])
    try:
        rows = client.get("/api/runtimes").json()["runtimes"]
        daemons = {r["daemon_id"] for r in rows}
        providers = {r["type"] for r in rows}
        assert daemons == {"d-live"}
        assert providers == {"claude"}
        assert len(rows) == 1
    finally:
        dispose()


# ── submit_task target check ──────────────────────────────────────


def _build_app_with_run_submit() -> tuple[TestClient, callable]:
    """Fresh DB; caller seeds rows and posts a submit."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_liveness_submit.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
    )

    async def _setup() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            await db.commit()
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    async def _dispose() -> None:
        await cfg._engine_ref.dispose()  # type: ignore[attr-defined]

    def _dispose_sync() -> None:
        asyncio.run(_dispose())

    return client, _dispose_sync


def test_submit_to_stale_daemon_rejected() -> None:
    """``--daemon <id>`` where the only matching row is stale → 400."""
    client, dispose = _build_app_with_run_submit()
    now = _utcnow_naive()
    stale = (now - timedelta(seconds=RUNTIME_STALE_SECONDS + 10)).strftime(TS_FMT)

    async def _seed() -> None:
        async with session_scope(client.app.state.session_factory) as db:
            db.add(_row("d-dead", "claude", last_seen=stale))
            await db.commit()

    asyncio.run(_seed())
    try:
        resp = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
            "target_daemon_id": "d-dead",
        })
        assert resp.status_code == 400, resp.text
        assert "no live runtime" in resp.json()["detail"]
    finally:
        dispose()


def test_submit_to_fresh_daemon_accepted() -> None:
    """Same target with a fresh row → 201 (control test for the previous one)."""
    client, dispose = _build_app_with_run_submit()
    now = _utcnow_naive()
    fresh = now.strftime(TS_FMT)

    async def _seed() -> None:
        async with session_scope(client.app.state.session_factory) as db:
            db.add(_row("d-live", "claude", last_seen=fresh))
            await db.commit()

    asyncio.run(_seed())
    try:
        resp = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
            "target_daemon_id": "d-live",
        })
        assert resp.status_code == 201, resp.text
    finally:
        dispose()


def test_submit_to_mixed_daemon_accepted_if_any_row_fresh() -> None:
    """A daemon with one stale row and one fresh row is still alive."""
    client, dispose = _build_app_with_run_submit()
    now = _utcnow_naive()
    fresh = now.strftime(TS_FMT)
    stale = (now - timedelta(seconds=RUNTIME_STALE_SECONDS + 10)).strftime(TS_FMT)

    async def _seed() -> None:
        async with session_scope(client.app.state.session_factory) as db:
            db.add(_row("d-mixed", "claude", last_seen=stale))
            db.add(_row("d-mixed", "codex", last_seen=fresh))
            await db.commit()

    asyncio.run(_seed())
    try:
        resp = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "codex",
            "target_daemon_id": "d-mixed",
        })
        assert resp.status_code == 201, resp.text
    finally:
        dispose()


# ── startup sweep (lifespan integration) ──────────────────────────


def test_startup_sweep_removes_stale_rows() -> None:
    """Boot the app with a pre-seeded stale row in the DB; the lifespan
    sweep must drop it before any request is served."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_liveness_startup.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
    )

    async def _seed() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            now = _utcnow_naive()
            stale = (now - timedelta(seconds=RUNTIME_STALE_SECONDS + 30)).strftime(TS_FMT)
            db.add(_row("d-dead", "claude", last_seen=stale))
            db.add(_row("d-live", "codex", last_seen=now.strftime(TS_FMT)))
            await db.commit()
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_seed())
    app = create_app(cfg)
    with TestClient(app) as client:
        rows = client.get("/api/runtimes").json()["runtimes"]
        # Live one must be present, stale one must be gone (list filters
        # at read time even if the sweep hadn't run — but the sweep also
        # physically removed it; either way it's invisible).
        assert {r["daemon_id"] for r in rows} == {"d-live"}
        # Confirm physical removal: hit the DB directly.
        async def _count() -> int:
            async with session_scope(client.app.state.session_factory) as db:
                rows = (await db.execute(select(RuntimeModel))).scalars().all()
                return len(rows)
        assert asyncio.run(_count()) == 1
    asyncio.run(cfg._engine_ref.dispose())  # type: ignore[attr-defined]


# ── DaemonWsServer.on_connect ─────────────────────────────────────


def test_on_connect_hook_fires_after_link_ready() -> None:
    """Hook is invoked exactly once per inbound connection, after the
    link is exposed as ``current_link`` (so the hook can use it)."""

    async def _run() -> None:
        from agt.daemon.ws_server import DaemonWsServer
        from agt.server.services.auth_service import TaskToken

        port = _free_port()
        fired: list = []
        link_at_fire: list = []

        async def hook(link) -> None:
            fired.append(link)
            link_at_fire.append(link)

        ws_server = DaemonWsServer(port=port, secret=SECRET, on_connect=hook)
        await ws_server.start()
        try:
            import websockets
            token = TaskToken.create_server("server", SECRET)
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/daemon",
                additional_headers={"X-Server-Token": token},
                open_timeout=5,
            ) as ws:
                # The hook runs inside _handler after the server sets
                # current_link. Give it a brief moment to fire.
                for _ in range(40):
                    if fired:
                        break
                    await asyncio.sleep(0.05)
                assert len(fired) == 1
                # The link passed to the hook is the same one the server
                # exposed — they reference the same WsRpcLink instance.
                assert link_at_fire[0] is ws_server.current_link
                # And the hook sees the live, non-closed link.
                assert link_at_fire[0] is not None
                assert link_at_fire[0].closed is False
        finally:
            await ws_server.stop()

    asyncio.run(_run())


def test_on_connect_hook_exception_is_swallowed() -> None:
    """A buggy hook must not take the link down: the connection stays
    up, the server processes other frames normally."""

    async def _run() -> None:
        from agt.daemon.ws_server import DaemonWsServer
        from agt.server.services.auth_service import TaskToken

        port = _free_port()

        async def hook(_link) -> None:
            raise RuntimeError("boom")

        ws_server = DaemonWsServer(port=port, secret=SECRET, on_connect=hook)
        await ws_server.start()
        try:
            import websockets
            token = TaskToken.create_server("server", SECRET)
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/daemon",
                additional_headers={"X-Server-Token": token},
                open_timeout=5,
            ) as ws:
                # Server should still ack us with heartbeat_ack even
                # though the hook raised before it logged "server dial
                # accepted". (Order: handshake → link ready → hook →
                # log → block on read loop.)
                # Receive the handshake ack first.
                hello = await asyncio.wait_for(ws.recv(), timeout=2)
                import json
                assert json.loads(hello)["type"] == "heartbeat_ack"
                # Then wait briefly to confirm the server's _handler is
                # still running (not cancelled by the hook exception).
                for _ in range(20):
                    if ws_server.current_link is not None:
                        break
                    await asyncio.sleep(0.05)
                assert ws_server.current_link is not None
        finally:
            await ws_server.stop()

    asyncio.run(_run())


def test_on_connect_pushes_snapshot_over_reverse_dial() -> None:
    """End-to-end: Server dials daemon, daemon's on_connect pushes a
    snapshot via ``runtimes.register``, and the Server's runtimes table
    reflects it — without waiting for the 30s heartbeat."""

    async def _run() -> None:
        from agt.daemon.daemon import Daemon
        from agt.daemon.ws_server import DaemonWsServer
        from agt.runtime.models import AgentEntry, Runtime
        from agt.server.daemon_link import DaemonLink
        from agt.server.services.auth_service import TaskToken

        # Build a fake Daemon with one probed runtime, so the snapshot
        # has something to send.
        daemon = Daemon()
        entry = AgentEntry(
            command="claude", path="/usr/bin/claude",
            version="1.0.0", provider="claude", display_name="Claude",
        )
        daemon._runtimes["claude"] = Runtime(
            name="Claude (test)", type="claude", version="1.0.0",
            status="online", agent_entry=entry, device_name="t",
        )

        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path="/tmp/_liveness_on_connect.db", secret=SECRET,
            sessions_json_path="/tmp/_nonexistent_sessions_for_liveness.json",
        )
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        try:
            port = _free_port()

            async def push(link) -> None:
                # Mirrors the production helper in daemon/main.py.
                runtimes = [
                    {
                        "name": rt.name, "type": rt.type, "version": rt.version,
                        "status": rt.status, "device_name": rt.device_name,
                    }
                    for rt in daemon._runtimes.values()
                ]
                await link.call(
                    "runtimes.register", {"runtimes": runtimes}, timeout_ms=5000,
                )

            ws_server = DaemonWsServer(port=port, secret=SECRET, on_connect=push)
            await ws_server.start()

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id="d-on-connect", url=f"ws://127.0.0.1:{port}",
                secret=SECRET, session_factory=factory,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                for _ in range(60):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                assert link.connected

                # Wait briefly for the on_connect hook to land and the
                # upsert to commit.
                expected_daemon = "d-on-connect"
                seen = None
                for _ in range(60):
                    async with session_scope(factory) as db:
                        rows = (await db.execute(
                            select(RuntimeModel).where(
                                RuntimeModel.daemon_id == expected_daemon,
                            )
                        )).scalars().all()
                        if rows and rows[0].provider == "claude":
                            seen = rows[0]
                            break
                    await asyncio.sleep(0.05)
                assert seen is not None, "on_connect did not push snapshot"
                assert seen.device_name == "t"
            finally:
                shutdown.set()
                link_task.cancel()
                try:
                    await asyncio.wait_for(link_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
                await ws_server.stop()
        finally:
            await engine.dispose()

    asyncio.run(_run())


# Touch the imports to avoid unused warnings if a refactor moves them.
_ = WSRPCUnavailable