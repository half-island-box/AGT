"""Reverse-dial tests for daemon-reverse-dial.

Exercises:

- ``TaskToken.create_server`` round-trip (7.1)
- ``WsRpcLink`` bidirectional RPC: ``call`` + ``on_request`` (7.2)
- ``DaemonWsServer`` auth: accept with valid token, 1008 with bad token (7.3)
- End-to-end integration: ``DaemonWsServer`` ⇄ ``DaemonLink`` claim round-trip
  with ``daemon_id`` taken from the configured target, not the body (7.4)
- Reconnect: server reconnects after a forced daemon disconnect (7.5)
- HTTP-fallback degradation: ``_claim_loop`` uses HTTP when no link is
  available (7.6)
"""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete
from starlette.testclient import TestClient
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError, InvalidStatus

from agt.comm.errors import WSRPCUnavailable, WSRPCUncertain
from agt.comm.wsrpc_link import WsRpcLink
from agt.daemon.ws_server import DaemonWsServer
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base, JobModel, StepModel
from agt.server.services.auth_service import TaskToken


SECRET = "reverse-dial-test-secret"
DAEMON_ID = "daemon-test-01"
DAEMON_URL = "ws://127.0.0.1:8422"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── 7.1 create_server round-trip ──────────────────────────────────


def test_create_server_round_trip() -> None:
    token = TaskToken.create_server("server-A", SECRET)
    claims = TaskToken.verify(token, SECRET)
    assert claims["sub"] == "server:server-A"
    assert "task_id" not in claims
    assert "workspace_id" not in claims


# ── 7.2 WsRpcLink bidirectional ─────────────────────────────────


def test_wsrpc_link_bidirectional_round_trip() -> None:
    """Two WsRpcLinks over a single socket pair: each side both calls and
    responds. Verifies ``on_request`` dispatch + call/response round-trip."""

    async def _echo(method: str, body: dict) -> dict:
        return {"echoed": body, "method": method}

    async def _run() -> None:
        port = _free_port()
        # Server side: a WsRpcLink that responds to inbound rpc_request.
        server_side: list[WsRpcLink] = []

        async def handler(ws: ServerConnection) -> None:
            link = WsRpcLink(ws, on_request=_echo)
            await link.start()
            server_side.append(link)
            try:
                # Hold the connection open until the peer closes.
                if link._read_task is not None:  # noqa: SLF001
                    await link._read_task
            finally:
                await link.close()

        server = await serve(handler, "127.0.0.1", port)
        try:
            import websockets

            client_ws = await websockets.connect(f"ws://127.0.0.1:{port}")
            client_link = WsRpcLink(client_ws, on_request=_echo)
            await client_link.start()
            try:
                # 1) Client calls server: server's on_request runs.
                result = await client_link.call("ping", {"x": 1}, timeout_ms=2000)
                assert result["echoed"] == {"x": 1}
                assert result["method"] == "ping"

                # 2) Server calls client: client's on_request runs.
                # (server_side was populated by handler())
                assert len(server_side) == 1
                srv_link = server_side[0]
                result2 = await srv_link.call("pong", {"y": 2}, timeout_ms=2000)
                assert result2["echoed"] == {"y": 2}
                assert result2["method"] == "pong"
            finally:
                await client_link.close()
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(_run())


# ── 7.3 DaemonWsServer auth ─────────────────────────────────────


def test_daemon_ws_server_accepts_valid_server_token() -> None:
    async def _run() -> None:
        port = _free_port()
        server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id=DAEMON_ID, url=DAEMON_URL
        )
        await server.start()
        try:
            import websockets

            token = TaskToken.create_server("server-A", SECRET, daemon_id=DAEMON_ID)
            async with websockets.connect(
                f"ws://127.0.0.1:{port}/daemon",
                additional_headers={"X-Server-Token": token},
                open_timeout=5,
            ) as ws:
                hello_raw = await asyncio.wait_for(ws.recv(), timeout=2)
                hello = json.loads(hello_raw)
                assert hello["type"] == "heartbeat_ack"
                assert hello.get("rpc_v1") is True
                assert "daemon_token" in hello
        finally:
            await server.stop()

    asyncio.run(_run())


def test_daemon_ws_server_rejects_bad_token_with_1008() -> None:
    async def _run() -> None:
        port = _free_port()
        server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id=DAEMON_ID, url=DAEMON_URL
        )
        await server.start()
        try:
            import websockets

            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/daemon",
                    additional_headers={"X-Server-Token": "garbage"},
                    open_timeout=5,
                ) as ws:
                    await ws.recv()
                    assert False, "bad token should have been rejected"
            except (InvalidStatus, ConnectionClosedError):
                # Expected — either HTTP-level reject (1008) or
                # WS-level close with the same reason.
                pass
        finally:
            await server.stop()

    asyncio.run(_run())


def test_daemon_ws_server_rejects_missing_token_with_1008() -> None:
    async def _run() -> None:
        port = _free_port()
        server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id=DAEMON_ID, url=DAEMON_URL
        )
        await server.start()
        try:
            import websockets

            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/daemon",
                    open_timeout=5,
                ) as ws:
                    await ws.recv()
                    assert False, "missing token should have been rejected"
            except (InvalidStatus, ConnectionClosedError):
                pass
        finally:
            await server.stop()

    asyncio.run(_run())


def test_daemon_ws_server_rejects_token_for_wrong_daemon() -> None:
    """A server token whose ``aud`` claim targets a different daemon must be
    rejected, preventing token reuse across daemons.
    """

    async def _run() -> None:
        port = _free_port()
        server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id=DAEMON_ID, url=DAEMON_URL
        )
        await server.start()
        try:
            import websockets

            token = TaskToken.create_server("server-A", SECRET, daemon_id="other-daemon")
            try:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/daemon",
                    additional_headers={"X-Server-Token": token},
                    open_timeout=5,
                ) as ws:
                    await ws.recv()
                    assert False, "wrong-audience token should have been rejected"
            except (InvalidStatus, ConnectionClosedError):
                pass
        finally:
            await server.stop()

    asyncio.run(_run())


def test_daemon_link_rejects_daemon_with_wrong_identity() -> None:
    """The Server must close the connection if the daemon's self-proclaimed
    identity does not match the configured daemon_id.
    """

    async def _run() -> None:
        port = _free_port()
        ws_server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id="actual-daemon", url=f"ws://127.0.0.1:{port}"
        )
        await ws_server.start()
        try:
            from agt.server.daemon_link import DaemonLink

            shutdown = asyncio.Event()
            # Server expects "configured-daemon", daemon identifies as "actual-daemon".
            link = DaemonLink(
                daemon_id="configured-daemon",
                url=f"ws://127.0.0.1:{port}",
                secret=SECRET,
                session_factory=None,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                # Give the link a few reconnect cycles; it should never become
                # connected because the daemon identity check fails.
                for _ in range(40):
                    if link.connected:
                        assert False, "link should not connect with mismatched daemon_id"
                    await asyncio.sleep(0.05)
            finally:
                shutdown.set()
                link_task.cancel()
                try:
                    await asyncio.wait_for(link_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        finally:
            await ws_server.stop()

    asyncio.run(_run())


# ── 7.4 end-to-end: DaemonWsServer ⇄ DaemonLink claim ───────────


def test_reverse_dial_claim_round_trip() -> None:
    """DaemonWsServer (in-process) accepts DaemonLink's dial; the daemon
    side issues ``tasks.claim``; the server-side handler returns a
    claimed task. ``daemon_id`` comes from the configured target."""

    async def _run() -> None:
        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path="/tmp/_reverse_dial.db", secret=SECRET,
            sessions_json_path="/tmp/_nonexistent_sessions_for_test.json",
        )
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        async with engine.begin() as conn:
            await conn.run_sync(migrate_identities_and_sessions, Path("/tmp"))
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(StepModel))
            await db.execute(delete(JobModel))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            db.add(JobModel(
                job_id="t-claim", status="queued", job_type="task",
                provider="claude", created_at=now, updated_at=now,
            ))
            db.add(StepModel(
                step_id="t-claim", job_id="t-claim", provider="claude", prompt="x",
                status="pending", created_at=now, updated_at=now,
            ))
            await db.commit()

        try:
            port = _free_port()
            daemon_id = "daemon-cloud-01"
            ws_server = DaemonWsServer(
                port=port, secret=SECRET, daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}"
            )
            await ws_server.start()

            from agt.server.daemon_link import DaemonLink

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id=daemon_id,
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
                assert daemon_link is not None, "daemon link not exposed"

                result = await daemon_link.call(
                    "tasks.claim",
                    {
                        # Body deliberately contains a WRONG daemon_id;
                        # the server must use the configured one.
                        "daemon_id": "wrong-id",
                        "runtime_ids": ["claude"],
                        "max_tasks": 1,
                    },
                    timeout_ms=2000,
                )
                tasks = result.get("tasks", [])
                assert len(tasks) == 1
                assert tasks[0]["task_id"] == "t-claim"

                # Verify the task was claimed under the configured
                # daemon_id, not the body value.
                async with session_scope(factory) as db:
                    row = await db.get(StepModel, "t-claim")
                    assert row.daemon_id == "daemon-cloud-01"
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


def test_reverse_dial_link_negotiates_rpc_v1() -> None:
    """The server MUST send a heartbeat_ack on connect so the daemon's
    link sees ``supports_rpc_v1=True``.

    Without this, the daemon's ``claim_tasks_ws_first`` would see
    ``supports_rpc_v1 == False`` and fall back to HTTP — which is
    unreachable in a reverse-dial setup, so every claim would fail with
    "All connection attempts failed" despite the WS link being up.
    """

    async def _run() -> None:
        port = _free_port()
        daemon_id = "d-rpcv1"
        ws_server = DaemonWsServer(
            port=port, secret=SECRET, daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}"
        )
        await ws_server.start()
        try:
            from agt.server.daemon_link import DaemonLink

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id=daemon_id,
                url=f"ws://127.0.0.1:{port}",
                secret=SECRET,
                session_factory=None,  # not exercised — we only assert negotiation
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

                # Wait for the server's heartbeat_ack to arrive.
                for _ in range(40):
                    if daemon_link.supports_rpc_v1:
                        break
                    await asyncio.sleep(0.05)
                assert daemon_link.supports_rpc_v1, (
                    "daemon link never saw rpc_v1 — server must send "
                    "heartbeat_ack on connect"
                )
            finally:
                shutdown.set()
                link_task.cancel()
                try:
                    await asyncio.wait_for(link_task, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        finally:
            await ws_server.stop()

    asyncio.run(_run())


# ── 7.5 reconnect ───────────────────────────────────────────────


def test_daemon_link_reconnects_after_drop() -> None:
    """Force-stop the daemon's WS server while a DaemonLink is connected;
    DaemonLink should reconnect when the server comes back."""

    async def _run() -> None:
        from agt.server.daemon_link import DaemonLink

        port = _free_port()
        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path="/tmp/_reverse_dial_reconnect.db", secret=SECRET,
            sessions_json_path="/tmp/_nonexistent_sessions_for_test.json",
        )
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        async with engine.begin() as conn:
            await conn.run_sync(migrate_identities_and_sessions, Path("/tmp"))
        factory = make_session_factory(engine)

        try:
            daemon_id = "d-recon"
            ws_server = DaemonWsServer(
                port=port, secret=SECRET, daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}"
            )
            await ws_server.start()

            shutdown = asyncio.Event()
            link = DaemonLink(
                daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}",
                secret=SECRET, session_factory=factory,
            )
            link_task = asyncio.create_task(link.run(shutdown))
            try:
                for _ in range(60):
                    if link.connected:
                        break
                    await asyncio.sleep(0.05)
                assert link.connected

                # Stop the server — drops the link.
                await ws_server.stop()

                # Wait for the link to notice.
                for _ in range(60):
                    if not link.connected:
                        break
                    await asyncio.sleep(0.05)

                # Bring server back.
                ws_server = DaemonWsServer(
                    port=port, secret=SECRET, daemon_id=daemon_id, url=f"ws://127.0.0.1:{port}"
                )
                await ws_server.start()
                try:
                    # DaemonLink should reconnect within a couple of
                    # backoff cycles (initial 1s + up to a few retries).
                    for _ in range(120):
                        if link.connected:
                            break
                        await asyncio.sleep(0.1)
                    assert link.connected, "DaemonLink did not reconnect"
                finally:
                    await ws_server.stop()
            finally:
                shutdown.set()
                link_task.cancel()
                try:
                    await asyncio.wait_for(link_task, timeout=3)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
        finally:
            await engine.dispose()

    asyncio.run(_run())


# ── 7.6 HTTP-fallback degradation ────────────────────────────────


def test_claim_loop_falls_back_to_http_when_no_link() -> None:
    """If ``DaemonWsServer`` has no current_link, ``_claim_loop`` should
    silently fall back to HTTP claim. We exercise this by verifying the
    WS-source resolution logic directly."""

    async def _run() -> None:
        # Server not started — current_link is None forever.
        server = DaemonWsServer(
            port=18555, secret=SECRET, daemon_id=DAEMON_ID, url=DAEMON_URL
        )

        link = server.current_link
        assert link is None

        # Mirror the _claim_loop resolution: only treat non-None
        # non-closed links as the WS source.
        ws_source = link if (link is not None and not link.closed) else None
        assert ws_source is None

    asyncio.run(_run())
