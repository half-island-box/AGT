"""Remote session history echo (``--resume <sid> --daemon=X``).

The transcript lives on the daemon machine (``~/.claude`` / ``~/.codex``).
This covers the whole chain that prints it back in the CLI:

- ``DaemonWsServer._handle_session_history`` — daemon-side reader.
- ``DaemonLink.get_session_history`` — server→daemon RPC.
- ``GET /api/sessions/{sid}/history`` — server endpoint (resolve → forward).
- ``ServerClient.get_session_history`` — client method.
- ``_print_remote_resumed_history`` — CLI rendering.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from agt.agent_history import Message
from agt.comm.client import ServerClient
from agt.comm.errors import RPCRejected, WSRPCUnavailable
from agt.daemon.ws_server import DaemonWsServer
from agt.server.config import ServerConfig
from agt.server.daemon_link import DaemonLink
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, TaskModel

SECRET = "remote-history-test-secret"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Daemon: session.history handler ──────────────────────────────────


class TestDaemonSessionHistoryHandler:
    def test_returns_serialized_messages(self) -> None:
        server = DaemonWsServer(port=0, secret="x")

        def _fake_load(provider, session_id):
            assert provider == "claude"
            assert session_id == "s1"
            return [
                Message(role="user", content="hello", timestamp=1.0),
                Message(role="assistant", content="hi", timestamp=2.0),
            ]

        with patch("agt.daemon.ws_server.load_history", _fake_load):
            result = asyncio.run(
                server._handle_session_history(
                    {"provider": "claude", "session_id": "s1"}
                )
            )
        assert result["not_implemented"] is False
        assert result["messages"] == [
            {"role": "user", "content": "hello", "timestamp": 1.0},
            {"role": "assistant", "content": "hi", "timestamp": 2.0},
        ]

    def test_not_implemented_provider(self) -> None:
        server = DaemonWsServer(port=0, secret="x")

        def _fake_load(provider, session_id):
            raise NotImplementedError("no reader for cursor")

        with patch("agt.daemon.ws_server.load_history", _fake_load):
            result = asyncio.run(
                server._handle_session_history(
                    {"provider": "cursor", "session_id": "s1"}
                )
            )
        assert result["not_implemented"] is True
        assert result["messages"] == []

    def test_missing_fields_rejected(self) -> None:
        server = DaemonWsServer(port=0, secret="x")

        async def _run() -> None:
            with pytest.raises(ValueError, match="validation_error"):
                await server._handle_session_history({})

        asyncio.run(_run())


# ── Server: DaemonLink.get_session_history ───────────────────────────


class _MockLink:
    def __init__(self, *, supports: bool = True, result: dict | None = None) -> None:
        self.closed = False
        self._supports = supports
        self._result = result if result is not None else {}
        self.calls: list[tuple[str, dict, int]] = []

    @property
    def supports_rpc_v1(self) -> bool:
        return self._supports

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((method, body, timeout_ms))
        return self._result


class TestDaemonLinkGetSessionHistory:
    def _link(self) -> DaemonLink:
        return DaemonLink(
            daemon_id="d1", url="ws://example/daemon", secret="s",
            session_factory=None,
        )

    def test_sends_session_history_rpc(self) -> None:
        link = self._link()
        link._link = _MockLink(result={"messages": []})  # type: ignore[assignment]

        result = asyncio.run(link.get_session_history("s1", "claude"))

        assert result == {"messages": []}
        assert link._link.calls == [  # type: ignore[attr-defined]
            ("session.history", {"session_id": "s1", "provider": "claude"}, 8000)
        ]

    def test_raises_when_link_down(self) -> None:
        link = self._link()
        with pytest.raises(WSRPCUnavailable):
            asyncio.run(link.get_session_history("s1", "claude"))


# ── Server: GET /api/sessions/{sid}/history ──────────────────────────


def _build_app(tmp_path: Path):
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

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return client, factory, _dispose


async def _seed_task(
    factory, *, task_id, session_id, daemon_id, provider="claude",
) -> None:
    now = _now()
    async with factory() as session:
        session.add(TaskModel(
            task_id=task_id, provider=provider, prompt="hi",
            status="completed", daemon_id=daemon_id, session_id=session_id,
            created_at=now, updated_at=now,
        ))
        await session.commit()


class _FakeDaemonLink:
    """Stand-in for DaemonLink in app.state.daemon_links."""

    def __init__(self, *, connected: bool = True, result=None, raise_exc=None) -> None:
        self._connected = connected
        self._result = result if result is not None else {}
        self._raise_exc = raise_exc
        self.calls: list[tuple[str, str]] = []

    @property
    def connected(self) -> bool:
        return self._connected

    async def get_session_history(self, session_id: str, provider: str) -> dict:
        self.calls.append((session_id, provider))
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._result


class TestSessionHistoryRoute:
    def test_unknown_session_404(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/sessions/nope/history")
            assert resp.status_code == 404
        finally:
            dispose()

    def test_no_owning_daemon_503(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id=None,
            ))
            resp = client.get("/api/sessions/s1/history")
            assert resp.status_code == 503
        finally:
            dispose()

    def test_daemon_offline_503(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
            ))
            # No link registered for d1 → offline.
            resp = client.get("/api/sessions/s1/history")
            assert resp.status_code == 503
        finally:
            dispose()

    def test_forwards_to_daemon_and_returns_messages(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
                provider="codex",
            ))
            fake = _FakeDaemonLink(result={
                "messages": [
                    {"role": "user", "content": "hello", "timestamp": 1.0},
                ],
                "not_implemented": False,
            })
            client.app.state.daemon_links = {"d1": fake}

            resp = client.get("/api/sessions/s1/history")
            assert resp.status_code == 200
            body = resp.json()
            assert body["session_id"] == "s1"
            assert body["not_implemented"] is False
            assert body["messages"] == [
                {"role": "user", "content": "hello", "timestamp": 1.0},
            ]
            # The daemon was asked with the resolved provider, not the CLI's.
            assert fake.calls == [("s1", "codex")]
        finally:
            dispose()

    def test_daemon_rejection_502(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
            ))
            fake = _FakeDaemonLink(
                raise_exc=RPCRejected(0, {"type": "unknown_method"})
            )
            client.app.state.daemon_links = {"d1": fake}
            resp = client.get("/api/sessions/s1/history")
            assert resp.status_code == 502
        finally:
            dispose()


# ── Client: get_session_history ──────────────────────────────────────


def _client_with(handler) -> ServerClient:
    import httpx
    client = ServerClient(base_url="http://x", daemon_id="cli")
    client._client = httpx.AsyncClient(  # type: ignore[assignment]
        transport=httpx.MockTransport(handler), base_url="http://x"
    )
    return client


class TestClientGetSessionHistory:
    def test_returns_body(self) -> None:
        import httpx

        def _handler(request):
            return httpx.Response(200, json={
                "session_id": "s1",
                "messages": [{"role": "user", "content": "hi", "timestamp": None}],
                "not_implemented": False,
            })

        client = _client_with(_handler)
        result = asyncio.run(client.get_session_history("s1"))
        assert result["session_id"] == "s1"
        assert result["messages"][0]["content"] == "hi"

    def test_404_returns_none(self) -> None:
        import httpx

        def _handler(request):
            return httpx.Response(404, json={"detail": "no session for id"})

        client = _client_with(_handler)
        assert asyncio.run(client.get_session_history("s1")) is None

    def test_503_raises(self) -> None:
        import httpx

        def _handler(request):
            return httpx.Response(503, json={"detail": "daemon offline"})

        client = _client_with(_handler)
        with pytest.raises(RPCRejected) as exc:
            asyncio.run(client.get_session_history("s1"))
        assert exc.value.status == 503


# ── CLI: _print_remote_resumed_history ───────────────────────────────


def _history_stub(result=None, *, exc=None):
    """Patch over ``agt.cli.main.ServerClient`` returning ``result`` from
    ``get_session_history`` (or raising ``exc``)."""

    class _Stub:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get_session_history(self, session_id):
            if exc is not None:
                raise exc
            return result

    return _Stub


class TestPrintRemoteResumedHistory:
    def test_prints_messages(self, capsys) -> None:
        from agt.cli import main as cli_mod
        result = {
            "session_id": "s1",
            "messages": [
                {"role": "user", "content": "hello", "timestamp": 1.0},
                {"role": "assistant", "content": "hi there", "timestamp": 2.0},
            ],
            "not_implemented": False,
        }
        with patch("agt.cli.main.ServerClient", _history_stub(result)):
            cli_mod._print_remote_resumed_history("s1", "http://x")
        out = capsys.readouterr().out
        assert "history of" in out
        assert "> user: hello" in out
        assert "· assistant: hi there" in out
        assert "2 message(s)" in out

    def test_not_implemented_hint(self, capsys) -> None:
        from agt.cli import main as cli_mod
        result = {"messages": [], "not_implemented": True}
        with patch("agt.cli.main.ServerClient", _history_stub(result)):
            cli_mod._print_remote_resumed_history("s1", "http://x")
        out = capsys.readouterr().err
        assert "not yet implemented" in out

    def test_offline_hint(self, capsys) -> None:
        from agt.cli import main as cli_mod
        with patch("agt.cli.main.ServerClient",
                   _history_stub(exc=RPCRejected(503, {"detail": "daemon offline"}))):
            cli_mod._print_remote_resumed_history("s1", "http://x")
        out = capsys.readouterr().err
        assert "daemon offline" in out

    def test_missing_session_hint(self, capsys) -> None:
        from agt.cli import main as cli_mod
        with patch("agt.cli.main.ServerClient", _history_stub(None)):
            cli_mod._print_remote_resumed_history("s1", "http://x")
        out = capsys.readouterr().err
        assert "not found on Server" in out

    def test_empty_messages_hint(self, capsys) -> None:
        from agt.cli import main as cli_mod
        result = {"messages": [], "not_implemented": False}
        with patch("agt.cli.main.ServerClient", _history_stub(result)):
            cli_mod._print_remote_resumed_history("s1", "http://x")
        out = capsys.readouterr().err
        assert "no history available" in out
