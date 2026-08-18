"""Claim orchestration for remote daemons — WS-first, HTTP-fallback.

Why this module exists
----------------------
``WsRpcLink.call()`` (Step 3.5) raises one of three exceptions when a
claim call doesn't complete normally. Each exception means a different
thing for HTTP fallback safety:

- ``WSRPCUnavailable`` — frame never left the client. HTTP fallback is
  safe immediately.
- ``WSRPCUncertain``   — frame MAY have reached the server before the
  connection dropped. The server may have committed the claim. Falling
  back to HTTP right now would risk running the same task twice — the
  exact MUL-4257 scenario we're guarding against.
- ``RPCRejected``      — server explicitly rejected. Caller decides.

Safety window
-------------
After ``WSRPCUncertain``, the caller must wait::

    max(sent_at + server_timeout + grace, now + grace)

before any HTTP retry. The deadline is computed from
``WSRPCUncertain.frame_sent_at`` (not from ``now``) so a timeout that
fires long after the send still waits out the full server-side window.

``ClaimStateMachine`` tracks that deadline. ``claim_tasks_ws_first``
branches on each exception type and either falls back, opens the window,
or propagates.

The ``wsrpc`` argument is a :class:`WsRpcLink` — the reverse-dial link
that the Server dialed (Step 3.5). It exposes ``call(method, body,
timeout_ms)`` and ``supports_rpc_v1``.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain

if TYPE_CHECKING:
    from ..comm.client import ServerClient
    from ..comm.wsrpc_link import WsRpcLink

log = logging.getLogger("agt.daemon.ws_claim")


# `time.monotonic` is injected via a module attribute so unit tests can
# patch it without monkey-patching the stdlib.
_monotonic = time.monotonic


class ClaimStateMachine:
    """Tracks the active Uncertain safety window.

    The only thing consumers ask is "am I currently inside a window?" —
    that is exposed as ``blocked``. State is a single monotonic deadline;
    no explicit transitions, no transition log.
    """

    def __init__(self, server_timeout: int = 5, grace: int = 2) -> None:
        self._uncertain_until: float = 0.0
        self._safe_window: float = float(server_timeout + grace)

    @property
    def blocked(self) -> bool:
        return _monotonic() < self._uncertain_until

    def mark_uncertain(self, sent_at: float) -> None:
        """Open the safety window ending at ``sent_at + safe_window``."""
        self._uncertain_until = sent_at + self._safe_window

    def reset(self) -> None:
        """Clear any pending window."""
        self._uncertain_until = 0.0


async def claim_tasks_ws_first(
    *,
    wsrpc: "WsRpcLink | None",
    http_client: "ServerClient",
    state: ClaimStateMachine,
    runtime_ids: list[str],
    max_tasks: int,
) -> list[dict]:
    """Try WS claim first; fall back to HTTP only when safe.

    Branches:
      - ``wsrpc is None`` or ``not wsrpc.supports_rpc_v1`` or ``state.blocked``
        → skip WS, go straight to HTTP.
      - WS returns tasks → return them, don't call HTTP.
      - WS raises ``WSRPCUnavailable`` → call HTTP, return its result.
      - WS raises ``WSRPCUncertain`` → open the safety window
        (``state.mark_uncertain(sent_at)``) and return ``[]`` for this
        invocation. HTTP is NOT consulted during the window.
      - WS raises ``RPCRejected`` → re-raise; HTTP is NOT a fallback for
        an explicit server rejection.
    """
    if wsrpc is None or not wsrpc.supports_rpc_v1 or state.blocked:
        return await http_client.claim_tasks(timeout_seconds=30)

    try:
        result = await wsrpc.call(
            "tasks.claim",
            {"runtime_ids": list(runtime_ids), "max_tasks": max_tasks},
            timeout_ms=5000,
        )
        return list(result.get("tasks", []))
    except WSRPCUnavailable:
        # Frame never left the client — HTTP fallback is safe.
        return await http_client.claim_tasks(timeout_seconds=30)
    except WSRPCUncertain as exc:
        # Frame MAY have committed; open the window, skip HTTP this tick.
        state.mark_uncertain(exc.frame_sent_at)
        return []
    # RPCRejected propagates unchanged — server's verdict is final.


async def report_task_ws_first(
    *,
    wsrpc: "WsRpcLink | None",
    http_client: "ServerClient",
    state: ClaimStateMachine,
    task_id: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    status: str,
    session_id: str | None,
    resume_rejected: bool,
) -> bool:
    """Write a task result via WS first; HTTP fallback when not safe.

    Mirror of :func:`claim_tasks_ws_first` for the result-write path,
    but with two semantic differences that matter for retry safety:

    1. **Result writes are idempotent on the server side** — the task
       row's terminal fields get overwritten by whichever report lands
       last. So an Uncertain WS write followed by an HTTP retry can
       record the same result twice without ill effect (the latest one
       wins). We still honor the safety window to avoid hammering the
       server mid-frame, but the retry that follows isn't dangerous.

    2. **RPCRejected is NOT fatal here** — unlike claim, where an
       explicit rejection means "this task isn't for you" and HTTP
       fallback would be wrong. For reports, a rejection (e.g. 404 task
       not found) usually means the WS server's view of state is
       stale; falling back to HTTP may still succeed.

    Returns True if any path recorded the result (best-effort); the
    caller doesn't act on ``False`` beyond logging.
    """
    body = {
        "task_id": task_id,
        "exit_code": exit_code,
        "stdout": stdout,
        "stderr": stderr,
        "duration": duration,
        "status": status,
        "session_id": session_id,
        "resume_rejected": resume_rejected,
    }

    if wsrpc is not None and wsrpc.supports_rpc_v1 and not state.blocked:
        try:
            await wsrpc.call("tasks.report", body, timeout_ms=5000)
            return True
        except WSRPCUnavailable:
            # Frame never left the client — HTTP fallback is safe.
            pass
        except WSRPCUncertain as exc:
            # Mark the window so subsequent ticks of the claim/report
            # loop don't double-fire. We don't block-wait here — the
            # caller's outer scheduler will retry on the next tick,
            # which will fall through to HTTP once the window expires.
            state.mark_uncertain(exc.frame_sent_at)
            log.debug(
                "report over WS Uncertain; will retry via HTTP after "
                "safety window (task=%s)", task_id,
            )
            return False
        except RPCRejected as exc:
            # Server explicitly rejected (typically 404 task not found).
            # Still try HTTP — the server's view may have lagged and
            # HTTP can succeed. log at debug since both paths log on
            # their own failure modes.
            log.debug(
                "report over WS rejected (status=%s); falling back to HTTP",
                exc.status,
            )
            # fall through to HTTP

    # HTTP path / fallback. The result is already in our hands; failure
    # here means we couldn't deliver it. The daemon's caller logs a
    # single warning at the outer try/except.
    try:
        await http_client.report_task(
            task_id=task_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            status=status,
            session_id=session_id,
            resume_rejected=resume_rejected,
        )
        return True
    except Exception as exc:
        log.warning("HTTP report failed for task %s: %s", task_id, exc)
        return False


__all__ = ["ClaimStateMachine", "claim_tasks_ws_first", "report_task_ws_first"]
