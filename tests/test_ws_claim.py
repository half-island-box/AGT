"""ClaimStateMachine + claim_tasks_ws_first tests.

The state machine is tested by patching ``ws_claim._monotonic`` so we
don't depend on real wall-clock sleeps. The orchestrator is tested
against stub ``WsRpcLink`` / ``ServerClient`` objects that record
calls and raise on cue — no socket, no DB.
"""

from __future__ import annotations

import asyncio

import pytest

from agt.comm.errors import (
    RPCRejected,
    WSRPCUnavailable,
    WSRPCUncertain,
)
from agt.daemon import ws_claim
from agt.daemon.ws_claim import ClaimStateMachine, claim_tasks_ws_first


# ── State machine ──────────────────────────────────────────────────


def test_state_machine_initial_unblocked() -> None:
    """Fresh ClaimStateMachine has no pending window."""
    sm = ClaimStateMachine()
    assert sm.blocked is False


def test_state_machine_mark_uncertain_sets_deadline_from_sent_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Window deadline is computed from sent_at, not from now."""
    fake_now = [1000.0]
    monkeypatch.setattr(ws_claim, "_monotonic", lambda: fake_now[0])

    sm = ClaimStateMachine(server_timeout=5, grace=2)
    sm.mark_uncertain(sent_at=998.0)  # sent 2 s before "now"
    assert sm.blocked is True

    fake_now[0] = 1004.99  # 1 ms before deadline (998 + 7 = 1005)
    assert sm.blocked is True

    fake_now[0] = 1005.01  # 10 ms past deadline
    assert sm.blocked is False


def test_state_machine_reset_clears_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """``reset()`` clears the window regardless of remaining time."""
    fake_now = [1000.0]
    monkeypatch.setattr(ws_claim, "_monotonic", lambda: fake_now[0])

    sm = ClaimStateMachine()
    sm.mark_uncertain(sent_at=1000.0)
    assert sm.blocked is True
    sm.reset()
    assert sm.blocked is False


# ── Orchestrator branches ───────────────────────────────────────────


class _StubWSRPC:
    """Records ``call()`` invocations and returns / raises as configured."""

    def __init__(
        self,
        *,
        supports_rpc_v1: bool = True,
        claim_result: list[dict] | None = None,
        claim_exc: BaseException | None = None,
    ) -> None:
        self._supports = supports_rpc_v1
        self._result = claim_result
        self._exc = claim_exc
        self.claim_calls = 0

    @property
    def supports_rpc_v1(self) -> bool:
        return self._supports

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        self.claim_calls += 1
        if self._exc is not None:
            raise self._exc
        return {"tasks": list(self._result or [])}


class _StubHTTP:
    """Records claim_tasks() invocations."""

    def __init__(self, result: list[dict] | None = None) -> None:
        self._result = result or []
        self.claim_calls = 0

    async def claim_tasks(self, timeout_seconds: int = 30) -> tuple[list[dict], list[str]]:  # noqa: ARG002
        self.claim_calls += 1
        return list(self._result), []


def test_orchestrator_ws_success_skips_http() -> None:
    """WS returns tasks → orchestrator returns them; HTTP is NOT called."""

    async def _run() -> None:
        ws = _StubWSRPC(claim_result=[{"task_id": "t1"}])
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine()
        result = await claim_tasks_ws_first(
            wsrpc=ws, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([{"task_id": "t1"}], [])
        assert ws.claim_calls == 1
        assert http.claim_calls == 0

    asyncio.run(_run())


def test_orchestrator_ws_unavailable_falls_back_to_http() -> None:
    """WSUnavailable → call HTTP; return its result."""

    async def _run() -> None:
        ws = _StubWSRPC(claim_exc=WSRPCUnavailable("no conn"))
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine()
        result = await claim_tasks_ws_first(
            wsrpc=ws, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([{"task_id": "h1"}], [])
        assert ws.claim_calls == 1
        assert http.claim_calls == 1
        assert state.blocked is False  # Unavailable must NOT open the window

    asyncio.run(_run())


def test_orchestrator_ws_uncertain_marks_window_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Uncertain → mark window from sent_at; return []; HTTP NOT called."""
    # Freeze monotonic at a known value so the deadline math is deterministic.
    fake_now = [1000.0]
    monkeypatch.setattr(ws_claim, "_monotonic", lambda: fake_now[0])

    async def _run() -> None:
        sent_at = 1000.0  # "now"
        ws = _StubWSRPC(claim_exc=WSRPCUncertain("timeout", frame_sent_at=sent_at))
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine(server_timeout=5, grace=2)
        result = await claim_tasks_ws_first(
            wsrpc=ws, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([], [])
        assert ws.claim_calls == 1
        assert http.claim_calls == 0  # the critical anti-MUL-4257 invariant
        assert state.blocked is True
        assert state._uncertain_until == sent_at + 7  # noqa: SLF001

    asyncio.run(_run())


def test_orchestrator_ws_rejected_propagates() -> None:
    """RPCRejected is re-raised unchanged; HTTP NOT consulted."""

    async def _run() -> None:
        ws = _StubWSRPC(claim_exc=RPCRejected(400, {"message": "bad"}))
        http = _StubHTTP()
        state = ClaimStateMachine()
        with pytest.raises(RPCRejected) as ei:
            await claim_tasks_ws_first(
                wsrpc=ws, http_client=http, state=state,
                runtime_ids=[], max_tasks=1,
            )
        assert ei.value.status == 400
        assert http.claim_calls == 0
        assert state.blocked is False

    asyncio.run(_run())


def test_orchestrator_skips_ws_when_state_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """state.blocked=True → even with supports_rpc_v1, skip WS, go HTTP."""
    fake_now = [1000.0]
    monkeypatch.setattr(ws_claim, "_monotonic", lambda: fake_now[0])

    async def _run() -> None:
        ws = _StubWSRPC(supports_rpc_v1=True, claim_result=[{"task_id": "w1"}])
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine()
        state.mark_uncertain(sent_at=fake_now[0])  # opens a 7-second window
        assert state.blocked is True
        result = await claim_tasks_ws_first(
            wsrpc=ws, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([{"task_id": "h1"}], [])
        assert ws.claim_calls == 0
        assert http.claim_calls == 1

    asyncio.run(_run())


def test_orchestrator_skips_ws_when_not_connected() -> None:
    """supports_rpc_v1=False → skip WS, go HTTP."""

    async def _run() -> None:
        ws = _StubWSRPC(supports_rpc_v1=False, claim_result=[{"task_id": "w1"}])
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine()
        result = await claim_tasks_ws_first(
            wsrpc=ws, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([{"task_id": "h1"}], [])
        assert ws.claim_calls == 0
        assert http.claim_calls == 1

    asyncio.run(_run())


def test_orchestrator_ws_is_none() -> None:
    """``wsrpc=None`` (the default-flag-off path) → just go HTTP."""

    async def _run() -> None:
        http = _StubHTTP(result=[{"task_id": "h1"}])
        state = ClaimStateMachine()
        result = await claim_tasks_ws_first(
            wsrpc=None, http_client=http, state=state,
            runtime_ids=[], max_tasks=1,
        )
        assert result == ([{"task_id": "h1"}], [])
        assert http.claim_calls == 1

    asyncio.run(_run())

