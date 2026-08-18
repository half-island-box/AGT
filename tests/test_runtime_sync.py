"""Runtime sync over the reverse-dial WS channel.

Verifies that a reverse-dial daemon can push its probe snapshot into the
server's ``runtimes`` table via the ``runtimes.register`` RPC — the same
operation the HTTP ``register`` / ``heartbeat`` endpoints perform, but
over the server-dialed WebSocket link (which is the only reachable
direction when the server is behind NAT).

Also covers the shared ``upsert_daemon_runtimes`` service directly.
"""

from __future__ import annotations

import asyncio
import socket
from datetime import datetime, timezone

from sqlalchemy import delete, select
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.models import Base, RuntimeModel
from agt.server.services.runtime_service import upsert_daemon_runtimes


SECRET = "runtime-sync-test-secret"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _snapshot(*providers: str) -> list[dict]:
    return [
        {"name": p, "type": p, "version": "1.0.0", "status": "online", "device_name": "ws-x"}
        for p in providers
    ]


def _fresh_engine():
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_runtime_sync.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_test.json",
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(RuntimeModel))
            await db.commit()

    asyncio.run(_setup())
    return engine, cfg


# ── shared service unit tests ────────────────────────────────────


def test_upsert_daemon_runtimes_replaces_and_stamps_last_seen() -> None:
    engine, cfg = _fresh_engine()
    factory = make_session_factory(engine)

    async def _run() -> None:
        async with session_scope(factory) as db:
            # First snapshot: two runtimes.
            n = await upsert_daemon_runtimes(
                db, daemon_id="d1", runtimes=_snapshot("claude", "codex"),
            )
            assert n == 2
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(RuntimeModel).where(RuntimeModel.daemon_id == "d1")
            )).scalars().all()
            assert {r.provider for r in rows} == {"claude", "codex"}
            assert all(r.last_seen for r in rows)  # last_seen stamped

        async with session_scope(factory) as db:
            # Second snapshot: replace with a different set.
            n = await upsert_daemon_runtimes(
                db, daemon_id="d1", runtimes=_snapshot("claude"),
            )
            assert n == 1
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(RuntimeModel).where(RuntimeModel.daemon_id == "d1")
            )).scalars().all()
            assert {r.provider for r in rows} == {"claude"}

        async with session_scope(factory) as db:
            # Empty snapshot → rows removed.
            n = await upsert_daemon_runtimes(db, daemon_id="d1", runtimes=[])
            assert n == 0
        async with session_scope(factory) as db:
            rows = (await db.execute(
                select(RuntimeModel).where(RuntimeModel.daemon_id == "d1")
            )).scalars().all()
            assert rows == []

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_upsert_isolates_daemons() -> None:
    engine, cfg = _fresh_engine()
    factory = make_session_factory(engine)

    async def _run() -> None:
        async with session_scope(factory) as db:
            await upsert_daemon_runtimes(db, daemon_id="d1", runtimes=_snapshot("claude"))
        async with session_scope(factory) as db:
            await upsert_daemon_runtimes(db, daemon_id="d2", runtimes=_snapshot("codex"))
        # d2's upsert must not disturb d1's rows.
        async with session_scope(factory) as db:
            d1 = (await db.execute(
                select(RuntimeModel).where(RuntimeModel.daemon_id == "d1")
            )).scalars().all()
            d2 = (await db.execute(
                select(RuntimeModel).where(RuntimeModel.daemon_id == "d2")
            )).scalars().all()
            assert {r.provider for r in d1} == {"claude"}
            assert {r.provider for r in d2} == {"codex"}

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── reverse-dial integration: DaemonWsServer ⇄ DaemonLink ────────


def test_reverse_dial_runtimes_register() -> None:
    """A daemon pushes its snapshot over the server-dialed link; the
    server's ``runtimes`` table reflects it, keyed by the configured
    ``daemon_id`` (not the body)."""

    engine, cfg = _fresh_engine()
    factory = make_session_factory(engine)

    async def _run() -> None:
        try:
            from agt.daemon.ws_server import DaemonWsServer
            from agt.server.daemon_link import DaemonLink

            port = _free_port()
            ws_server = DaemonWsServer(port=port, secret=SECRET)
            await ws_server.start()

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id="daemon-cloud-01",
                url=f"ws://127.0.0.1:{port}",
                secret=SECRET,
                session_factory=factory,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                for _ in range(60):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                assert link.connected, "DaemonLink did not connect"

                daemon_link = ws_server.current_link
                assert daemon_link is not None

                # Wait for rpc_v1 negotiation (server sends heartbeat_ack).
                for _ in range(40):
                    if daemon_link.supports_rpc_v1:
                        break
                    await asyncio.sleep(0.05)
                assert daemon_link.supports_rpc_v1

                # Daemon pushes its snapshot over the link.
                result = await daemon_link.call(
                    "runtimes.register",
                    {"runtimes": _snapshot("claude", "codex")},
                    timeout_ms=2000,
                )
                assert result["registered"] == 2

                # Server's runtimes table now has the rows, keyed by the
                # configured daemon_id.
                async with session_scope(factory) as db:
                    rows = (await db.execute(
                        select(RuntimeModel).where(RuntimeModel.daemon_id == "daemon-cloud-01")
                    )).scalars().all()
                    assert {r.provider for r in rows} == {"claude", "codex"}
                    assert all(r.device_name == "ws-x" for r in rows)
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


def test_reverse_dial_runtimes_register_rejects_bad_body() -> None:
    """A non-list ``runtimes`` body → validation_error, DB untouched."""

    engine, cfg = _fresh_engine()
    factory = make_session_factory(engine)

    async def _run() -> None:
        try:
            from agt.daemon.ws_server import DaemonWsServer
            from agt.server.daemon_link import DaemonLink

            port = _free_port()
            ws_server = DaemonWsServer(port=port, secret=SECRET)
            await ws_server.start()

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id="daemon-cloud-01",
                url=f"ws://127.0.0.1:{port}",
                secret=SECRET,
                session_factory=factory,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                for _ in range(60):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                assert link.connected

                daemon_link = ws_server.current_link
                for _ in range(40):
                    if daemon_link.supports_rpc_v1:
                        break
                    await asyncio.sleep(0.05)

                from agt.comm.errors import RPCRejected
                try:
                    await daemon_link.call(
                        "runtimes.register",
                        {"runtimes": "not-a-list"},
                        timeout_ms=2000,
                    )
                    assert False, "should have raised RPCRejected"
                except RPCRejected as exc:
                    assert exc.status == 0  # validation_error has no HTTP status
                    assert exc.body["type"] == "validation_error"
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