"""Daemon identity mirror sync: ensure_identity_md + state file IO.

These tests cover three layers:

1. **State file IO** (``load_state`` / ``save_state``) — atomic write,
   missing-file tolerance, corrupt-JSON tolerance.
2. **Ensure algorithm** — ``ensure_identity_md`` with ``_fetch_identity``
   monkeypatched to canned responses. Verifies the cache hit / miss /
   stale paths and the network-failure fallback.
3. **HTTP transport** — ``_fetch_identity`` end-to-end via
   ``httpx.MockTransport``: header injection, non-2xx surfacing.

The test ``Path.home()`` is monkeypatched to ``tmp_path`` so no real
``~/.agt`` is ever touched. Async tests follow the repo's ``asyncio.run``
convention (no ``pytest-asyncio`` dependency).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from agt.daemon import identity_sync
from agt.daemon.identity_render import local_agent_md_path
from agt.daemon.identity_sync import (
    _fetch_identity,
    ensure_identity_md,
    load_state,
    save_state,
    state_file_path,
)


# ── Helpers ────────────────────────────────────────────────────────


def _home_fixture(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``Path.home()`` at ``tmp_path`` (test sandbox only)."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    return tmp_path


def _identity(updated_at: str = "2026-08-25 14:23:11") -> dict:
    return {
        "did": "did:agt:code-reviewer",
        "daemon_id": "d-uuid5-form",
        "name": "Code Reviewer",
        "description": "Reviews PRs.",
        "capabilities": ["python", "code-review"],
        "status": "active",
        "visibility": "private",
        "created_by": "alice",
        "created_at": "2026-08-20 10:00:00",
        "updated_at": updated_at,
    }


def _async_return(value):
    """Stand-in for an async function: returns ``value`` when awaited."""

    async def _fn(**_kw):
        return value

    return _fn


def _async_raise(exc):
    async def _fn(**_kw):
        raise exc
    return _fn


def _bind_async_client(monkeypatch, transport):
    """Replace ``httpx.AsyncClient`` in identity_sync's namespace with a
    factory that injects the given MockTransport. AsyncClient is used
    only as an async context manager in ``_fetch_identity`` so a
    callable factory that returns a real instance works.

    The original ``AsyncClient`` is captured before the monkeypatch —
    otherwise the factory's closure would resolve ``httpx.AsyncClient``
    to itself and recurse.
    """
    orig_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return orig_client(*args, **kwargs)

    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", _factory)


# ── State file IO ──────────────────────────────────────────────────


class TestStateFileIO:
    def test_state_file_path_under_home(self, monkeypatch, tmp_path) -> None:
        _home_fixture(monkeypatch, tmp_path)
        assert state_file_path() == tmp_path / ".agt" / "daemon-identity-cache.json"

    def test_load_state_when_missing_returns_empty(self, monkeypatch, tmp_path) -> None:
        _home_fixture(monkeypatch, tmp_path)
        assert load_state() == {"version": 1, "identities": {}}

    def test_save_then_load_roundtrips(self, monkeypatch, tmp_path) -> None:
        _home_fixture(monkeypatch, tmp_path)
        state = {
            "version": 1,
            "identities": {"did:agt:x": "2026-08-25"},
        }
        save_state(state)
        assert load_state() == state

    def test_load_state_corrupt_json_returns_empty(self, monkeypatch, tmp_path) -> None:
        _home_fixture(monkeypatch, tmp_path)
        path = state_file_path()
        path.parent.mkdir(parents=True)
        path.write_text("{not valid json,,,", encoding="utf-8")
        assert load_state() == {"version": 1, "identities": {}}

    def test_load_state_wrong_shape_returns_empty(self, monkeypatch, tmp_path) -> None:
        _home_fixture(monkeypatch, tmp_path)
        path = state_file_path()
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
        assert load_state() == {"version": 1, "identities": {}}

    def test_save_state_is_atomic(self, monkeypatch, tmp_path) -> None:
        """No leftover ``.tmp`` files after save_state (atomic replace)."""
        _home_fixture(monkeypatch, tmp_path)
        state = {"version": 1, "identities": {"did:agt:x": "t"}}
        save_state(state)
        siblings = sorted(p.name for p in state_file_path().parent.iterdir())
        assert siblings == ["daemon-identity-cache.json"]


# ── ensure_identity_md: algorithm branches ─────────────────────────


class TestEnsureIdentityMd:
    def test_first_call_fetches_and_writes(self, monkeypatch, tmp_path) -> None:
        """No local file, no cache → fetch from server, write file,
        record updated_at in cache, return local path."""
        _home_fixture(monkeypatch, tmp_path)
        monkeypatch.setattr(
            identity_sync, "_fetch_identity",
            _async_return(_identity("2026-08-25 14:23:11")),
        )

        target = local_agent_md_path("did:agt:code-reviewer")
        result = asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="secret",
            identity_did="did:agt:code-reviewer",
        ))

        assert result == target
        assert target.exists(), "AGENTS.md should be created on first run"
        state = load_state()
        assert state["identities"]["did:agt:code-reviewer"] == "2026-08-25 14:23:11"
        body = target.read_text(encoding="utf-8")
        assert "did:agt:code-reviewer" in body
        assert "Code Reviewer" in body

    def test_cache_hit_does_not_rewrite(self, monkeypatch, tmp_path) -> None:
        """Cache has the same updated_at as the server → file unchanged
        (user customizations preserved) and state untouched.

        Note: the fetch itself still runs — the comparison requires the
        server's current ``updated_at``. The cache only saves the write
        and re-render; it doesn't save the RTT. Avoiding the RTT would
        require push-based invalidation (out of scope here)."""
        _home_fixture(monkeypatch, tmp_path)
        target = local_agent_md_path("did:agt:code-reviewer")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("LOCAL CUSTOM CONTENT", encoding="utf-8")
        save_state({
            "version": 1,
            "identities": {
                "did:agt:code-reviewer": "2026-08-25 14:23:11",
            },
        })

        fetch_called = False

        async def _spy(**_kw):
            nonlocal fetch_called
            fetch_called = True
            return _identity("2026-08-25 14:23:11")

        monkeypatch.setattr(identity_sync, "_fetch_identity", _spy)

        result = asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        assert result == target
        assert target.read_text(encoding="utf-8") == "LOCAL CUSTOM CONTENT"
        assert fetch_called, "fetch must run for the updated_at comparison"
        # State unchanged (no advance — same value we started with).
        assert load_state()["identities"]["did:agt:code-reviewer"] == "2026-08-25 14:23:11"

    def test_stale_cache_triggers_refetch(self, monkeypatch, tmp_path) -> None:
        """Cache says older updated_at than server → re-render + write."""
        _home_fixture(monkeypatch, tmp_path)
        target = local_agent_md_path("did:agt:code-reviewer")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("STALE LOCAL CONTENT", encoding="utf-8")
        save_state({
            "version": 1,
            "identities": {"did:agt:code-reviewer": "2026-08-20"},
        })

        monkeypatch.setattr(
            identity_sync, "_fetch_identity",
            _async_return(_identity("2026-08-25 14:23:11")),
        )

        result = asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        assert result == target
        body = target.read_text(encoding="utf-8")
        assert "STALE LOCAL CONTENT" not in body
        assert "did:agt:code-reviewer" in body
        state = load_state()
        assert state["identities"]["did:agt:code-reviewer"] == "2026-08-25 14:23:11"

    def test_fetch_failure_without_local_returns_none(self, monkeypatch, tmp_path) -> None:
        """Network down, no local file → return None (caller will skip
        identity injection, matching the pre-fix silent-skip behavior)."""
        _home_fixture(monkeypatch, tmp_path)
        monkeypatch.setattr(
            identity_sync, "_fetch_identity",
            _async_raise(httpx.ConnectError("network down")),
        )

        result = asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        assert result is None
        assert load_state() == {"version": 1, "identities": {}}

    def test_fetch_failure_with_local_file_returns_local(
        self, monkeypatch, tmp_path,
    ) -> None:
        """Network down, but local file exists → use it as best-effort."""
        _home_fixture(monkeypatch, tmp_path)
        monkeypatch.setattr(
            identity_sync, "_fetch_identity",
            _async_raise(httpx.ConnectError("network down")),
        )

        target = local_agent_md_path("did:agt:code-reviewer")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("OFFLINE CONTENT", encoding="utf-8")

        result = asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        assert result == target
        # Content unchanged (we didn't fetch, so we don't overwrite).
        assert target.read_text(encoding="utf-8") == "OFFLINE CONTENT"

    def test_cache_records_updated_at_for_repeated_calls(
        self, monkeypatch, tmp_path,
    ) -> None:
        """After ensure, state has the right structure for re-use."""
        _home_fixture(monkeypatch, tmp_path)
        monkeypatch.setattr(
            identity_sync, "_fetch_identity",
            _async_return(_identity("2026-08-25 14:23:11")),
        )

        asyncio.run(ensure_identity_md(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        state = load_state()
        assert state["version"] == 1
        assert "did:agt:code-reviewer" in state["identities"]
        assert state["identities"]["did:agt:code-reviewer"] == "2026-08-25 14:23:11"


# ── HTTP transport ─────────────────────────────────────────────────


class TestFetchIdentity:
    def test_sends_secret_header_and_parses_body(self, monkeypatch) -> None:
        captured: dict = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            captured["url"] = str(request.url)
            return httpx.Response(200, json={
                "did": "did:agt:code-reviewer",
                "updated_at": "2026-08-25 14:23:11",
            })

        _bind_async_client(monkeypatch, httpx.MockTransport(_handler))

        result = asyncio.run(_fetch_identity(
            server_url="http://server", server_secret="topsecret",
            identity_did="did:agt:code-reviewer",
        ))

        assert result["did"] == "did:agt:code-reviewer"
        assert captured["headers"]["x-server-secret"] == "topsecret"
        assert captured["url"].endswith("/api/identities/did:agt:code-reviewer")

    def test_empty_secret_omits_header(self, monkeypatch) -> None:
        captured: dict = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"did": "x", "updated_at": "t"})

        _bind_async_client(monkeypatch, httpx.MockTransport(_handler))

        asyncio.run(_fetch_identity(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        ))

        assert "x-server-secret" not in captured["headers"]

    def test_non_2xx_raises(self, monkeypatch) -> None:
        def _handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"detail": "not found"})

        _bind_async_client(monkeypatch, httpx.MockTransport(_handler))

        coro = _fetch_identity(
            server_url="http://server", server_secret="",
            identity_did="did:agt:code-reviewer",
        )
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(coro)
