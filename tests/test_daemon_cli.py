"""Tests for ``agt daemon list`` and ``agt daemon show`` (Phase 4).

Stubs ServerClient; verifies table rendering, offline visibility, and
exit codes.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


def _daemon(**overrides) -> dict:
    base = {
        "daemon_id": "d1",
        "device_name": "laptop-a",
        "providers": [
            {"provider": "claude", "name": "claude (d1)", "version": "2.1.0",
             "status": "online", "last_seen": "2026-08-20 10:00:00"},
        ],
        "runtime_count": 1,
        "last_seen": "2026-08-20 10:00:00",
        "online": True,
    }
    base.update(overrides)
    return base


def _stub(calls: dict, **returns):
    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def _call(self, name, *args, **kwargs):
            calls.setdefault(name, []).append((args, kwargs))
            value = returns.get(name)
            if isinstance(value, Exception):
                raise value
            return value() if callable(value) and not isinstance(value, type) else value

        async def list_daemons(self):
            return await self._call("list_daemons")

        async def get_daemon(self, daemon_id):
            return await self._call("get_daemon", daemon_id)

    return _StubClient


def _invoke(args, stub) -> object:
    with patch("agt.cli.main.ServerClient", stub):
        runner = CliRunner()
        return runner.invoke(cli, args)


class TestList:
    def test_list_renders_table_with_offline_row(self) -> None:
        daemons = [
            _daemon(daemon_id="d1", online=True),
            _daemon(daemon_id="d2", device_name="old-box",
                    providers=[{"provider": "codex", "name": "codex (d2)",
                                "version": "0.9.0", "status": "online",
                                "last_seen": "2026-08-19 03:00:00"}],
                    runtime_count=1, online=False,
                    last_seen="2026-08-19 03:00:00"),
        ]
        result = _invoke(["daemon", "list"], _stub({}, list_daemons=lambda: daemons))
        assert result.exit_code == 0, result.output
        assert "2 daemon(s)" in result.output
        assert "d1" in result.output and "d2" in result.output
        # Offline row: ONLINE=no, last_seen still visible.
        assert "no" in result.output
        assert "2026-08-19 03:00:00" in result.output

    def test_list_empty(self) -> None:
        result = _invoke(["daemon", "list"], _stub({}, list_daemons=lambda: []))
        assert result.exit_code == 0
        assert "No daemons registered" in result.output

    def test_list_forwards_server_url(self) -> None:
        calls: dict = {}
        result = _invoke(
            ["daemon", "list", "--server-url", "http://example:9999"],
            _stub(calls, list_daemons=lambda: []),
        )
        assert result.exit_code == 0, result.output
        assert calls["list_daemons"] == [((), {})]


class TestShow:
    def test_show_renders_details(self) -> None:
        daemon = _daemon(providers=[
            {"provider": "claude", "name": "claude (d1)", "version": "2.1.0",
             "status": "online", "last_seen": "2026-08-20 10:00:00"},
            {"provider": "codex", "name": "codex (d1)", "version": "1.0.0",
             "status": "online", "last_seen": "2026-08-20 10:01:00"},
        ], runtime_count=2)
        result = _invoke(["daemon", "show", "d1"], _stub({}, get_daemon=lambda: daemon))
        assert result.exit_code == 0, result.output
        assert "d1" in result.output
        assert "laptop-a" in result.output
        assert "yes" in result.output
        assert "2.1.0" in result.output and "1.0.0" in result.output

    def test_show_not_found_exits_2(self) -> None:
        calls: dict = {}
        result = _invoke(
            ["daemon", "show", "ghost"], _stub(calls, get_daemon=lambda: None)
        )
        assert result.exit_code == 2
        assert "not found" in result.output
        assert calls["get_daemon"] == [(("ghost",), {})]
