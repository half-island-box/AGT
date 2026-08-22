"""Tests for the ``agt identity`` command group (Phase 4 Step 1).

The commands are thin wrappers over ``ServerClient`` identity methods.
We stub ``ServerClient`` (same pattern as ``test_agt_task_list.py``) so
the CLI is exercised end-to-end without a real Server, verifying:

- flags → payload construction (defaults, slug None, capability lists)
- table/detail rendering
- exit codes: 0 success, 2 not-found/no-op, 1 server error
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


def _ident(**overrides) -> dict:
    base = {
        "did": "did:agt:code-reviewer",
        "daemon_id": "local",
        "name": "Code Reviewer",
        "description": "Python code review",
        "capabilities": ["code-review", "doc-style-audit"],
        "status": "active",
        "visibility": "private",
        "created_by": "user",
        "created_at": "2026-08-20T10:00:00",
        "updated_at": "2026-08-20T10:00:00",
    }
    base.update(overrides)
    return base


def _stub(calls: dict, **returns):
    """Build a ServerClient stub that records calls and returns canned data."""

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

        async def list_identities(self, daemon_id=None, status=None):
            return await self._call("list_identities", daemon_id=daemon_id, status=status)

        async def create_identity(self, payload):
            return await self._call("create_identity", payload)

        async def get_identity(self, did):
            return await self._call("get_identity", did)

        async def update_identity(self, did, payload):
            return await self._call("update_identity", did, payload)

        async def deprecate_identity(self, did):
            return await self._call("deprecate_identity", did)

        async def revoke_identity(self, did):
            return await self._call("revoke_identity", did)

        async def delete_identity(self, did):
            return await self._call("delete_identity", did)

        async def sync_identity_files(self, did):
            return await self._call("sync_identity_files", did)

    return _StubClient


def _invoke(args, stub) -> object:
    with patch("agt.cli.main.ServerClient", stub):
        runner = CliRunner()
        return runner.invoke(cli, args)


# ── list ───────────────────────────────────────────────────────────


def test_list_renders_table() -> None:
    calls: dict = {}
    stub = _stub(calls, list_identities=lambda: [_ident(), _ident(status="deprecated")])
    result = _invoke(["identity", "list"], stub)
    assert result.exit_code == 0, result.output
    assert "2 identit" in result.output
    assert "did:agt:code-reviewer" in result.output
    assert "deprecated" in result.output
    # No filters → both None.
    assert calls["list_identities"] == [((), {"daemon_id": None, "status": None})]


def test_list_empty() -> None:
    result = _invoke(["identity", "list"], _stub({}, list_identities=lambda: []))
    assert result.exit_code == 0
    assert "No identities" in result.output


def test_list_forwards_filters() -> None:
    calls: dict = {}
    stub = _stub(calls, list_identities=lambda: [])
    result = _invoke(
        ["identity", "list", "--daemon", "d1", "--status", "active"], stub
    )
    assert result.exit_code == 0, result.output
    assert calls["list_identities"] == [((), {"daemon_id": "d1", "status": "active"})]


# ── show ───────────────────────────────────────────────────────────


def test_show_renders_details() -> None:
    result = _invoke(
        ["identity", "show", "did:agt:code-reviewer"],
        _stub({}, get_identity=lambda: _ident()),
    )
    assert result.exit_code == 0, result.output
    assert "Code Reviewer" in result.output
    assert "code-review, doc-style-audit" in result.output
    assert "active" in result.output


def test_show_not_found_exits_2() -> None:
    result = _invoke(
        ["identity", "show", "did:agt:ghost"],
        _stub({}, get_identity=lambda: None),
    )
    assert result.exit_code == 2
    assert "not found" in result.output


# ── create ─────────────────────────────────────────────────────────


def test_create_builds_payload_with_defaults() -> None:
    calls: dict = {}
    stub = _stub(calls, create_identity=lambda: _ident())
    result = _invoke(
        ["identity", "create", "--name", "Code Reviewer",
         "--capability", "code-review", "--capability", "doc-style-audit"],
        stub,
    )
    assert result.exit_code == 0, result.output
    assert "Created identity" in result.output
    payload = calls["create_identity"][0][0][0]
    assert payload == {
        "daemon_id": "local",       # default namespace
        "name": "Code Reviewer",
        "slug": None,               # derived server-side from name
        "description": "",
        "capabilities": ["code-review", "doc-style-audit"],
        "visibility": "private",
    }


def test_create_explicit_flags_forwarded() -> None:
    calls: dict = {}
    stub = _stub(calls, create_identity=lambda: _ident())
    result = _invoke(
        ["identity", "create", "--name", "X", "--slug", "xx",
         "--daemon-id", "d9", "--description", "desc",
         "--visibility", "public"],
        stub,
    )
    assert result.exit_code == 0, result.output
    payload = calls["create_identity"][0][0][0]
    assert payload["slug"] == "xx"
    assert payload["daemon_id"] == "d9"
    assert payload["description"] == "desc"
    assert payload["visibility"] == "public"


def test_create_server_error_exits_1() -> None:
    result = _invoke(
        ["identity", "create", "--name", "X"],
        _stub({}, create_identity=RuntimeError("already exists")),
    )
    assert result.exit_code == 1
    assert "already exists" in result.output


# ── update ─────────────────────────────────────────────────────────


def test_update_builds_partial_payload() -> None:
    calls: dict = {}
    stub = _stub(calls, update_identity=lambda: _ident())
    did = "did:agt:code-reviewer"
    result = _invoke(
        ["identity", "update", did, "--name", "Renamed",
         "--add-capability", "refactor", "--remove-capability", "doc-style-audit"],
        stub,
    )
    assert result.exit_code == 0, result.output
    assert "Updated identity" in result.output
    assert "sync-files" in result.output  # hint that AGENT.md is unchanged
    payload = calls["update_identity"][0][0][1]
    assert payload == {
        "name": "Renamed",
        "add_capabilities": ["refactor"],
        "remove_capabilities": ["doc-style-audit"],
    }


def test_update_without_flags_exits_2() -> None:
    result = _invoke(
        ["identity", "update", "did:agt:x"], _stub({})
    )
    assert result.exit_code == 2
    assert "Nothing to update" in result.output


# ── deprecate / revoke / delete / sync-files ───────────────────────


def test_deprecate_revoke_sync_output() -> None:
    did = "did:agt:code-reviewer"
    cases = (
        ("deprecate", "deprecate_identity", "Deprecated"),
        ("revoke", "revoke_identity", "Revoked"),
        ("sync-files", "sync_identity_files", "Synced"),
    )
    for cmd, method, verb in cases:
        result = _invoke(
            ["identity", cmd, did], _stub({}, **{method: lambda: _ident()})
        )
        assert result.exit_code == 0, result.output
        assert verb in result.output


def test_delete_success_and_not_found() -> None:
    did = "did:agt:code-reviewer"
    ok = _invoke(["identity", "delete", did], _stub({}, delete_identity=lambda: True))
    assert ok.exit_code == 0, ok.output
    assert "Deleted identity" in ok.output

    missing = _invoke(
        ["identity", "delete", did], _stub({}, delete_identity=lambda: False)
    )
    assert missing.exit_code == 2
    assert "not found" in missing.output


def test_delete_conflict_error_exits_1() -> None:
    result = _invoke(
        ["identity", "delete", "did:agt:x"],
        _stub({}, delete_identity=RuntimeError("task(s) still reference")),
    )
    assert result.exit_code == 1
    assert "reference" in result.output
