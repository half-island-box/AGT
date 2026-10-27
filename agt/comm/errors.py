"""WS-RPC error types — semantic exception hierarchy.

The hierarchy encodes **decisions**, not events. Callers branch on the
exception type to decide whether HTTP fallback is safe.
"""

from __future__ import annotations

import time


class WSRPCError(Exception):
    """Base class for all WS-RPC related errors."""


class WSRPCUnavailable(WSRPCError):
    """The WS connection is unavailable, write buffer is full, or a frame
    definitely did NOT reach the server.

    HTTP fallback is safe.
    """


class WSRPCUncertain(WSRPCError):
    """A frame MAY have reached the server before the connection dropped.
    The server may have committed.

    Do NOT immediately retry via HTTP; wait out the safety window
    (``server_timeout + grace``) first. Use ``frame_sent_at`` to compute the
    window deadline.
    """

    def __init__(self, message: str, frame_sent_at: float | None = None) -> None:
        super().__init__(message)
        # Wall-clock monotonic seconds when the frame was handed to the
        # websocket library's send buffer. ``None`` if the timeout happened
        # before send completed (degenerate; treated as 'now' by consumers).
        self.frame_sent_at: float = (
            frame_sent_at if frame_sent_at is not None else time.monotonic()
        )


class RPCRejected(WSRPCError):
    """The server explicitly rejected the request.

    ``status`` and ``body`` carry the server's response for diagnostics.
    Retry is permitted but the caller usually should not — the rejection
    is typically about input validation or authorization.
    """

    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body
        super().__init__(f"RPC rejected with status {status}: {body}")


__all__ = ["WSRPCError", "WSRPCUnavailable", "WSRPCUncertain", "RPCRejected"]