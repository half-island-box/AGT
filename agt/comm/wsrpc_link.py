"""WsRpcLink — bidirectional JSON-RPC over a single WebSocket connection.

Either side of the connection can:

- **Send requests** via ``call(method, body, timeout_ms)``; the returned
  future resolves with the matched ``rpc_response`` result (or rejects
  with ``RPCRejected`` / ``WSRPCUncertain``).
- **Respond to inbound requests** via the ``on_request`` callback, which
  is dispatched for every received ``rpc_request`` frame and whose
  return value is sent back as an ``rpc_response``.

This is the single JSON-RPC link used by both ends of the reverse-dial
topology (Step 3.5 / daemon-reverse-dial): the server dials the daemon
and responds to ``tasks.claim`` frames, while the daemon issues those
claims over the same socket.
frames sent by the daemon over the same socket.

Wire format (unchanged from Step 3.2)::

    request  = {"type":"rpc_request",
                "payload":{"request_id","method","body","timeout_ms"}}
    response = {"type":"rpc_response",
                "payload":{"request_id","result"|"error"}}
    hello    = {"type":"heartbeat_ack","rpc_v1":true}

Raise semantics (unchanged from Step 3.3)::

    WSRPCUnavailable — frame never left the client. HTTP retry is safe.
    WSRPCUncertain   — frame may have committed; caller MUST wait the
                       safety window (``timeout_ms + grace``) before retry.
    RPCRejected      — peer explicitly rejected with ``error``.

Generation counter (``_generation``) is bumped on every reconnect; stale
responses from a previous generation are dropped, so a response can't
resolve a future created after a reconnect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Union

from websockets.exceptions import ConnectionClosed

from .errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain

log = logging.getLogger("agt.comm.wsrpc")

# on_request can be sync or async; we await the result either way.
OnRequest = Callable[[str, dict], Union[dict, Awaitable[dict]]]


class WsRpcLink:
    """Bidirectional JSON-RPC-over-WebSocket link.

    Constructed around an already-open ``websockets`` connection. Call
    ``start()`` to begin the read loop; call ``close()`` to tear down.

    ``on_request`` is invoked for every inbound ``rpc_request`` frame.
    Its return value (sync or coroutine) is sent back as the matching
    ``rpc_response``. Exceptions raised by ``on_request`` are converted
    into ``rpc_response`` carrying ``{"type":"handler_error","message":...}``
    so a buggy handler can't take the socket down.
    """

    def __init__(
        self,
        ws: Any,
        *,
        on_request: OnRequest | None = None,
        grace: float = 2.0,
        generation: int = 0,
    ) -> None:
        self._ws = ws
        self._on_request = on_request
        self._grace = grace

        self._pending: dict[str, asyncio.Future[dict]] = {}
        # ``generation`` is bumped on ``close()`` to invalidate stale
        # responses. Callers that reconnect can seed the next link with
        # ``prev + 1`` to keep the public counter monotonic.
        self._generation = generation
        self._rpc_v1_supported = False
        self._read_task: asyncio.Task | None = None
        self._closed = False

    # ── Public API ────────────────────────────────────────────────

    @property
    def ws(self) -> Any:
        return self._ws

    @property
    def supports_rpc_v1(self) -> bool:
        return self._rpc_v1_supported

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    async def start(self) -> None:
        """Begin the read loop. Idempotent."""
        if self._read_task is not None and not self._read_task.done():
            return
        self._read_task = asyncio.create_task(self._read_loop())

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        """Send one RPC request, await response, return the ``result`` body."""
        if self._ws is None or self._closed:
            raise WSRPCUnavailable("no connection")

        rid, fut = self._register_call()
        frame = self._encode_request(rid, method, body, timeout_ms)
        gen_at_call = self._generation

        sent_at = await self._send_or_raise(rid, frame)
        return await self._await_response(rid, fut, gen_at_call, sent_at, timeout_ms)

    async def close(self) -> None:
        """Shut down. All pending futures are rejected as Unavailable."""
        self._closed = True
        self._generation += 1
        for rid in list(self._pending):  # snapshot keys before mutating
            fut = self._pending.pop(rid)
            if not fut.done():
                fut.set_exception(WSRPCUnavailable("connection closed"))
        if self._read_task is not None and not self._read_task.done():
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):
                pass
            self._read_task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ── Call plumbing ─────────────────────────────────────────────

    def _register_call(self) -> tuple[str, asyncio.Future[dict]]:
        rid = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[dict] = loop.create_future()
        self._pending[rid] = fut
        return rid, fut

    @staticmethod
    def _encode_request(rid: str, method: str, body: dict, timeout_ms: int) -> str:
        return json.dumps({
            "type": "rpc_request",
            "payload": {
                "request_id": rid,
                "method": method,
                "body": body,
                "timeout_ms": timeout_ms,
            },
        })

    async def _send_or_raise(self, rid: str, frame: str) -> float:
        try:
            await self._ws.send(frame)
        except Exception as exc:
            self._pending.pop(rid, None)
            raise WSRPCUnavailable(f"send failed: {exc}") from exc
        return time.monotonic()

    async def _await_response(
        self,
        rid: str,
        fut: asyncio.Future[dict],
        gen_at_call: int,
        sent_at: float,
        timeout_ms: int,
    ) -> dict:
        try:
            return await asyncio.wait_for(
                fut, timeout=timeout_ms / 1000.0 + self._grace
            )
        except asyncio.TimeoutError as exc:
            self._pending.pop(rid, None)
            if self._generation != gen_at_call:
                raise WSRPCUnavailable("connection reset mid-call") from exc
            raise WSRPCUncertain(
                "timeout, outcome unknown",
                frame_sent_at=sent_at,
            ) from exc

    # ── Read loop + frame dispatch ────────────────────────────────

    async def _read_loop(self) -> None:
        """Demultiplex frames; dispatch inbound requests to ``on_request``."""
        gen = self._generation
        ws = self._ws
        if ws is None:
            return
        try:
            async for raw in ws:
                self._dispatch_frame(raw, gen)
        except ConnectionClosed as exc:
            log.info("WsRpcLink connection closed: %s", exc)
            self._fail_all_pending(WSRPCUnavailable(f"connection closed: {exc.code}"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("WsRpcLink read_loop crashed: %s", exc)
            self._fail_all_pending(WSRPCUnavailable(f"read loop crashed: {exc}"))

    def _dispatch_frame(self, raw: str, gen: int) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("WsRpcLink received non-JSON frame; ignoring")
            return

        mtype = msg.get("type")
        if mtype == "heartbeat_ack":
            self._handle_hello(msg, gen)
        elif mtype == "rpc_response":
            self._complete_call(msg.get("payload") or {}, gen)
        elif mtype == "rpc_request":
            # Inbound RPC — dispatch to on_request. We don't await here so
            # the read loop keeps draining frames while the handler runs.
            self._dispatch_inbound_request(msg.get("payload") or {}, gen)

    def _handle_hello(self, msg: dict, gen: int) -> None:
        if msg.get("rpc_v1"):
            self._rpc_v1_supported = True
            log.debug("WsRpcLink rpc_v1 negotiated (gen=%d)", gen)

    def _complete_call(self, payload: dict, gen: int) -> None:
        rid = payload.get("request_id")
        if not isinstance(rid, str):
            return
        if gen != self._generation:
            log.debug("WsRpcLink dropping stale response rid=%s", rid)
            return
        fut = self._pending.pop(rid, None)
        if fut is None or fut.done():
            return
        if "error" in payload:
            fut.set_exception(self._build_rejection(payload["error"]))
        else:
            fut.set_result(payload.get("result", {}))

    def _build_rejection(self, error: Any) -> RPCRejected:
        if isinstance(error, dict):
            status = int(error.get("status", 0))
            return RPCRejected(status, error)
        return RPCRejected(0, {"message": str(error)})

    def fail_all_pending(self, reason: str) -> None:
        """Reject every pending future with ``WSRPCUnavailable(reason)``.

        Public helper so callers that reconnect can attach a meaningful
        reason ("connection reset") instead of the generic "connection
        closed" that ``close()`` uses.
        """
        self._fail_all_pending(WSRPCUnavailable(reason))

    def _fail_all_pending(self, exc: Exception) -> None:
        # Snapshot items first — we mutate _pending inside the loop.
        # ``list()`` is required (not redundant): popping during iteration
        # without a snapshot raises RuntimeError.
        for rid, fut in list(self._pending.items()):  # noqa: S7504
            if not fut.done():
                fut.set_exception(exc)
            self._pending.pop(rid, None)

    # ── Inbound request dispatch ─────────────────────────────────

    def _dispatch_inbound_request(self, payload: dict, gen: int) -> None:
        """Spawn a task to handle an inbound ``rpc_request`` and reply.

        Running as a separate task means a slow handler doesn't block the
        read loop. Errors are caught and turned into a structured
        response so a buggy handler can't take the socket down.
        """
        rid = payload.get("request_id")
        method = payload.get("method")
        body = payload.get("body") or {}

        if not isinstance(rid, str) or not isinstance(method, str):
            log.warning("WsRpcLink inbound rpc_request missing rid/method: %r", payload)
            return

        if gen != self._generation:
            log.debug("WsRpcLink dropping stale inbound request method=%s", method)
            return

        if self._on_request is None:
            # No handler registered — return an unknown_method error so
            # the caller doesn't hang on its pending future.
            asyncio.create_task(
                self._send_error_response(rid, {"type": "unknown_method", "message": method})
            )
            return

        asyncio.create_task(self._run_inbound(rid, method, body))

    async def _run_inbound(self, rid: str, method: str, body: dict) -> None:
        try:
            result = self._on_request(method, body)
            if hasattr(result, "__await__"):
                result = await result
            await self._send_result_response(rid, result)
        except ValueError as exc:
            # Handler-declared validation error — surface as error payload.
            payload = exc.args[0] if exc.args else str(exc)
            await self._send_error_response(rid, payload)
        except Exception as exc:
            log.exception("WsRpcLink inbound handler %s raised", method)
            await self._send_error_response(
                rid,
                {"type": "handler_error", "message": str(exc)},
            )

    async def _send_result_response(self, rid: str, result: Any) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "rpc_response",
                "payload": {"request_id": rid, "result": result if result is not None else {}},
            }))
        except Exception:
            log.warning("WsRpcLink failed to send result for rid=%s", rid)

    async def _send_error_response(self, rid: str, error: Any) -> None:
        if self._ws is None or self._closed:
            return
        try:
            await self._ws.send(json.dumps({
                "type": "rpc_response",
                "payload": {"request_id": rid, "error": error},
            }))
        except Exception:
            log.warning("WsRpcLink failed to send error for rid=%s", rid)


__all__ = ["WsRpcLink"]