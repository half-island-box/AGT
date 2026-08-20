"""Tests for the ``agt identity bind/unbind/bindings/default`` commands
(Phase 4 Step 2).

Stubs ServerClient (same pattern as test_identity_cli.py) and verifies:
- payload construction / flag forwarding
- triple-lookup → delete / set-default flow (unbind/default)
- table rendering and exit codes (0/1/2)
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


DID = "did:agt:local:reviewer"


def _binding(**overrides) -> dict:
    base = {
        "id": 1, "identity_did": DID, "daemon_id": "local",
        "provider": "claude", "priority": 0, "is_default": False,
        "created_at": "2026-08-20T10:00:00",
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

        async def list_bindings(self, did, daemon_id=None):
            return await self._call("list_bindings", did, daemon_id=daemon_id)

        async def create_binding(self, did, payload):
            return await self._call("create_binding", did, payload)

        async def delete_binding(self, did, binding_id):
            return await self._call("delete_binding", did, binding_id)

        async def set_default_binding(self, did, binding_id):
            return await self._call("set_default_binding", did, binding_id)

    return _StubClient


def _invoke(args, stub) -> object:
    with patch("agt.cli.main.ServerClient", stub):
        runner = CliRunner()
        return runner.invoke(cli, args)


# ── bind ───────────────────────────────────────────────────────────


class TestBind:
    def test_bind_builds_payload(self) -> None:
        calls: dict = {}
        stub = _stub(calls, create_binding=lambda: _binding())
        result = _invoke(
            ["identity", "bind", DID, "--daemon", "local", "--runtime", "claude"],
            stub,
        )
        assert result.exit_code == 0, result.output
        assert "Bound" in result.output
        payload = calls["create_binding"][0][0][1]
        assert payload == {
            "daemon_id": "local", "provider": "claude",
            "priority": 0, "is_default": False,
        }

    def test_bind_priority_and_default_forwarded(self) -> None:
        calls: dict = {}
        stub = _stub(calls, create_binding=lambda: _binding(id=7, is_default=True))
        result = _invoke(
            ["identity", "bind", DID, "--daemon", "local", "--runtime", "codex",
             "--priority", "10", "--default"],
            stub,
        )
        assert result.exit_code == 0, result.output
        assert "default" in result.output.lower()
        payload = calls["create_binding"][0][0][1]
        assert payload["priority"] == 10
        assert payload["is_default"] is True

    def test_bind_conflict_exits_1(self) -> None:
        result = _invoke(
            ["identity", "bind", DID, "--daemon", "local", "--runtime", "claude"],
            _stub({}, create_binding=RuntimeError("already exists")),
        )
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_bind_requires_flags(self) -> None:
        result = _invoke(["identity", "bind", DID], _stub({}))
        assert result.exit_code == 2  # missing --daemon/--runtime


# ── unbind ─────────────────────────────────────────────────────────


class TestUnbind:
    def test_unbind_looks_up_then_deletes(self) -> None:
        calls: dict = {}
        bindings = [
            _binding(id=3, provider="claude"),
            _binding(id=5, provider="codex"),
        ]
        stub = _stub(
            calls,
            list_bindings=lambda: bindings,
            delete_binding=lambda: True,
        )
        result = _invoke(
            ["identity", "unbind", DID, "--daemon", "local", "--runtime", "codex"],
            stub,
        )
        assert result.exit_code == 0, result.output
        assert "Unbound" in result.output
        assert calls["list_bindings"] == [((DID,), {"daemon_id": "local"})]
        assert calls["delete_binding"] == [((DID, 5), {})]  # id from lookup

    def test_unbind_not_found_exits_2(self) -> None:
        calls: dict = {}
        stub = _stub(calls, list_bindings=lambda: [_binding(provider="claude")])
        result = _invoke(
            ["identity", "unbind", DID, "--daemon", "local", "--runtime", "codex"],
            stub,
        )
        assert result.exit_code == 2
        assert "No binding" in result.output
        assert "delete_binding" not in calls  # never attempted


# ── bindings ───────────────────────────────────────────────────────


class TestBindings:
    def test_bindings_renders_table(self) -> None:
        bindings = [
            _binding(id=3, provider="claude", is_default=True),
            _binding(id=5, provider="codex", daemon_id="remote", priority=10),
        ]
        result = _invoke(
            ["identity", "bindings", DID], _stub({}, list_bindings=lambda: bindings)
        )
        assert result.exit_code == 0, result.output
        assert "2 binding(s)" in result.output
        assert "ID" in result.output and "DAEMON" in result.output
        assert "claude" in result.output and "codex" in result.output
        assert "yes" in result.output  # default marker

    def test_bindings_empty_friendly(self) -> None:
        result = _invoke(
            ["identity", "bindings", DID], _stub({}, list_bindings=lambda: [])
        )
        assert result.exit_code == 0
        assert "No bindings" in result.output
        assert "agt identity bind" in result.output

    def test_bindings_forwards_daemon_filter(self) -> None:
        calls: dict = {}
        stub = _stub(calls, list_bindings=lambda: [_binding()])
        result = _invoke(
            ["identity", "bindings", DID, "--daemon", "local"], stub
        )
        assert result.exit_code == 0, result.output
        assert calls["list_bindings"] == [((DID,), {"daemon_id": "local"})]


# ── default ────────────────────────────────────────────────────────


class TestDefault:
    def test_default_switches(self) -> None:
        calls: dict = {}
        bindings = [_binding(id=5, provider="codex")]
        stub = _stub(
            calls,
            list_bindings=lambda: bindings,
            set_default_binding=lambda: _binding(id=5, provider="codex", is_default=True),
        )
        result = _invoke(
            ["identity", "default", DID, "--daemon", "local", "--runtime", "codex"],
            stub,
        )
        assert result.exit_code == 0, result.output
        assert "Default for" in result.output
        assert calls["set_default_binding"] == [((DID, 5), {})]

    def test_default_not_found_exits_2(self) -> None:
        calls: dict = {}
        stub = _stub(calls, list_bindings=lambda: [_binding(provider="claude")])
        result = _invoke(
            ["identity", "default", DID, "--daemon", "local", "--runtime", "codex"],
            stub,
        )
        assert result.exit_code == 2
        assert "No binding" in result.output
        assert "set_default_binding" not in calls
