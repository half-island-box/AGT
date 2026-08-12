"""Phase 3 placeholder error types.

These exception classes exist so Phase 2 code can already reference them via
``from agt.comm.errors import WSRPCUncertain``. The full WS-RPC implementation
(where these become load-bearing) lands in Phase 3.
"""


class WSRPCError(Exception):
    """Base class for all WS-RPC related errors (Phase 3)."""


class WSRPCUnavailable(WSRPCError):
    """The WS connection is unavailable, write buffer is full, or a frame
    definitely did NOT reach the server. → HTTP fallback is safe.
    """


class WSRPCUncertain(WSRPCError):
    """A frame MAY have reached the server before the connection dropped.
    The server may have committed. → Do NOT immediately retry via HTTP;
    wait out the safety window (server-budget + grace) first.
    """


class RPCRejected(WSRPCError):
    """The server explicitly rejected the request (HTTP-style status >= 300).

    ``status`` and ``body`` carry the server's response for diagnostics.
    Retry via HTTP fallback is permitted.
    """

    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self.body = body
        super().__init__(f"RPC rejected with status {status}: {body}")


__all__ = ["WSRPCError", "WSRPCUnavailable", "WSRPCUncertain", "RPCRejected"]