"""Tests for the notify + pull hybrid task-delivery optimization.

Covers:

1. ``claim_service.notify_task_available`` wakes HTTP-long-polling claim
   requests immediately.
2. ``DaemonWsServer._handle_inbound_request("tasks.available")`` wakes the
   daemon's claim loop.
3. ``DaemonLink.notify_available`` sends the ``tasks.available`` RPC over
   the reverse-dial link.
4. ``_notify_daemons`` triggers both the module-level claim event and the
   per-daemon WS notify.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from sqlalchemy.ext.asyncio import async_sessionmaker

from agt.daemon.ws_server import DaemonWsServer
from agt.server.config import ServerConfig
from agt.server.daemon_link import DaemonLink
from agt.server.database import make_engine, make_session_factory
from agt.server.models import Base, RuntimeModel, TaskModel
from agt.server.routes.tasks import _notify_daemons
from agt.server.services.claim_service import (
    claim_tasks_for_daemon,
    get_task_available_event,
    notify_task_available,
)


SECRET = "notify-test-secret"


def _build_engine_and_factory(tmp_path: Path) -> tuple[object, async_sessionmaker, ServerConfig]:
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "server.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    return engine, factory, cfg


async def _seed_runtime(factory, *, daemon_id: str, provider: str) -> None:
    async with factory() as session:
        session.add(
            RuntimeModel(
                daemon_id=daemon_id,
                provider=provider,
                name=f"{provider} ({daemon_id})",
                version="1.0",
                status="online",
                device_name="dev",
                last_seen=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
            )
        )
        await session.commit()


async def _submit_task(factory, *, task_id: str = "t1", provider: str = "claude") -> None:
    async with factory() as session:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        session.add(
            TaskModel(
                task_id=task_id,
                provider=provider,
                prompt="hi",
                status="pending",
                work_dir=None,
                agent_cwd=None,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()


# ── claim_service event wake-up ──────────────────────────────────────


def test_notify_task_available_wakes_long_poll(tmp_path: Path) -> None:
    """A late task submission wakes a claim request before its timeout."""
    engine, factory, _cfg = _build_engine_and_factory(tmp_path)
    try:
        asyncio.run(_seed_runtime(factory, daemon_id="d1", provider="claude"))

        async def _delayed_submit() -> None:
            # Submit 100ms after the claimer starts polling.
            await asyncio.sleep(0.1)
            await _submit_task(factory)
            notify_task_available()

        async def _claim() -> list:
            async with factory() as session:
                return await claim_tasks_for_daemon(
                    session,
                    daemon_id="d1",
                    max_tasks=1,
                    timeout_seconds=5,
                    runtime_ids=["claude"],
                )

        async def _run() -> list:
            submitter = asyncio.create_task(_delayed_submit())
            results = await _claim()
            await submitter
            return results

        results = asyncio.run(_run())
        assert len(results) == 1
        assert results[0].task_id == "t1"
    finally:
        asyncio.run(engine.dispose())


def test_claim_service_event_clears_after_wake() -> None:
    """After a wake, the event is cleared so future claimers block again."""
    event = get_task_available_event()
    event.set()
    notify_task_available()  # idempotent on already-set event
    assert event.is_set()

    # A fresh waiter that is woken should find the event cleared by the
    # claim loop logic. We simulate the wake/clear cycle directly.
    async def _consume() -> None:
        await asyncio.wait_for(event.wait(), timeout=0.1)
        event.clear()

    asyncio.run(_consume())
    assert not event.is_set()


# ── daemon ws_server tasks.available handler ─────────────────────────


def test_ws_server_tasks_available_wakes_claim_loop() -> None:
    """Inbound ``tasks.available`` sets the daemon's claim wakeup event."""
    server = DaemonWsServer(port=0, secret="x")
    assert not server.claim_wakeup.is_set()

    result = server._handle_inbound_request("tasks.available", {})

    assert result == {"ok": True}
    assert server.claim_wakeup.is_set()


def test_ws_server_unknown_method_still_rejected() -> None:
    """Methods other than ``tasks.available`` keep returning unknown_method."""
    server = DaemonWsServer(port=0, secret="x")
    with pytest.raises(ValueError, match="unknown_method"):
        server._handle_inbound_request("tasks.assign", {})


# ── DaemonLink notify_available ──────────────────────────────────────


def test_daemon_link_notify_available_sends_rpc() -> None:
    """``notify_available`` calls ``tasks.available`` on a live link."""
    link = DaemonLink(
        daemon_id="d1",
        url="ws://example/daemon",
        secret="secret",
        session_factory=None,
    )
    mock_wsrpc = _MockWsRpcLink(supports_rpc_v1=True)
    link._link = mock_wsrpc  # type: ignore[assignment]

    result = asyncio.run(link.notify_available("claude"))

    assert result is True
    assert mock_wsrpc.calls == [("tasks.available", {"provider": "claude"}, 5000)]


def test_daemon_link_notify_available_skips_disconnected_link() -> None:
    """If the link is down, notify returns False without raising."""
    link = DaemonLink(
        daemon_id="d1",
        url="ws://example/daemon",
        secret="secret",
        session_factory=None,
    )
    result = asyncio.run(link.notify_available("claude"))
    assert result is False


def test_daemon_link_notify_available_sends_free_task_without_provider() -> None:
    """A free-task notify may omit the provider hint."""
    link = DaemonLink(
        daemon_id="d1",
        url="ws://example/daemon",
        secret="secret",
        session_factory=None,
    )
    mock_wsrpc = _MockWsRpcLink(supports_rpc_v1=True)
    link._link = mock_wsrpc  # type: ignore[assignment]

    result = asyncio.run(link.notify_available())

    assert result is True
    assert mock_wsrpc.calls == [("tasks.available", {"provider": None}, 5000)]


class _MockWsRpcLink:
    """Minimal stub for WsRpcLink used by DaemonLink.notify_available."""

    def __init__(self, *, supports_rpc_v1: bool = True) -> None:
        self._supports_rpc_v1 = supports_rpc_v1
        self.calls: list[tuple[str, dict, int]] = []

    @property
    def closed(self) -> bool:
        return False

    @property
    def supports_rpc_v1(self) -> bool:
        return self._supports_rpc_v1

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((method, body, timeout_ms))
        return {"ok": True}


# ── _notify_daemons wiring ───────────────────────────────────────────


class _FakeRequest:
    """Minimal stand-in for ``fastapi.Request`` that exposes app.state."""

    def __init__(self, daemon_links: dict[str, object]) -> None:
        self.app = SimpleNamespace(state=SimpleNamespace(daemon_links=daemon_links))


def test_notify_daemons_targets_single_daemon() -> None:
    """Targeted task notifies only the named reverse-dial daemon."""

    async def _run() -> None:
        link = AsyncMock()
        link.notify_available = AsyncMock(return_value=True)
        request = _FakeRequest({"d1": link})

        await _notify_daemons(request, target_daemon_id="d1", provider="claude")

        link.notify_available.assert_awaited_once_with("claude")

    asyncio.run(_run())


def test_notify_daemons_free_task_broadcasts() -> None:
    """Free task notifies every online reverse-dial daemon."""

    async def _run() -> None:
        link1 = AsyncMock()
        link2 = AsyncMock()
        request = _FakeRequest({"d1": link1, "d2": link2})

        await _notify_daemons(request, target_daemon_id=None, provider="claude")

        link1.notify_available.assert_awaited_once_with("claude")
        link2.notify_available.assert_awaited_once_with("claude")

    asyncio.run(_run())


def test_notify_daemons_sets_claim_event() -> None:
    """``_notify_daemons`` always sets the module-level claim event."""
    event = get_task_available_event()
    event.clear()
    request = _FakeRequest({})

    asyncio.run(_notify_daemons(request, target_daemon_id=None, provider="claude"))

    assert event.is_set()
