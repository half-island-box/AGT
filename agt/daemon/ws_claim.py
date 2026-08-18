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

import time
from typing import TYPE_CHECKING

from ..comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain

if TYPE_CHECKING:
    from ..comm.client import ServerClient
    from ..comm.wsrpc_link import WsRpcLink


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


__all__ = ["ClaimStateMachine", "claim_tasks_ws_first"]
