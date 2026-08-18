"""DaemonWsServer — daemon-side WebSocket server for reverse-dial (Step 3.5).

In the reverse-dial topology, the **Server** dials the daemon instead of
the other way around. The daemon needs to:

1. Bind a public WebSocket endpoint on ``0.0.0.0:<port>`` (default 8422).
2. Authenticate the caller — anyone on the network could connect, and we
   only want to accept connections from a Server that holds the shared
   HMAC secret.
3. Send a ``heartbeat_ack{rpc_v1:true}`` frame as capability negotiation.
4. Expose the resulting :class:`WsRpcLink` so the daemon's claim loop
   (``_claim_loop``) can issue ``tasks.claim`` over it.

Authentication uses :class:`TaskToken.verify` on an ``X-Server-Token``
header (signed with ``sub=server:<id>``). A failed token closes the
connection with code 1008 before any frames are read.

The daemon does **not** currently respond to inbound RPC requests —
future Step 3.6+ may add server-pushed ``tasks.assign``, but for now
the daemon only ever issues requests on this link. Inbound ``rpc_request``
frames are answered with an ``unknown_method`` error so the caller
doesn't hang on its pending future.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection, serve

from ..comm.errors import WSRPCUnavailable
from ..comm.wsrpc_link import WsRpcLink
from ..server.services.auth_service import TaskToken

log = logging.getLogger("agt.daemon.ws")


class DaemonWsServer:
    """Daemon-side WS server. Exposes ``current_link`` for the claim loop."""

    def __init__(self, port: int, secret: str) -> None:
        self._port = port
        self._secret = secret
        self._server: serve | None = None
        self.current_link: WsRpcLink | None = None
        self._link_lock = asyncio.Lock()

    async def start(self) -> None:
        """Bind the WS server on ``0.0.0.0:<port>`` and start serving."""
        self._server = await serve(
            self._handler,
            "0.0.0.0",
            self._port,
            ping_interval=20,
            ping_timeout=20,
        )
        log.info("DaemonWsServer listening on 0.0.0.0:%d", self._port)

    async def stop(self) -> None:
        """Tear down the server and any active link."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        async with self._link_lock:
            link = self.current_link
            self.current_link = None
        if link is not None:
            try:
                await link.close()
            except Exception as exc:
                log.warning("DaemonWsServer link close failed: %s", exc)

    async def _handler(self, ws: ServerConnection) -> None:
        """Per-connection handler: auth → handshake → expose link."""
        # 1. Auth: verify X-Server-Token. Use the raw header lookup before
        #    we ever read a frame, so an unauthenticated caller never
        #    triggers business logic.
        token = ws.request.headers.get("X-Server-Token")
        if not token:
            log.info("DaemonWsServer: missing X-Server-Token; closing 1008")
            await ws.close(code=1008, reason="token required")
            return
        try:
            TaskToken.verify(token, self._secret)
        except PermissionError as exc:
            log.info("DaemonWsServer: invalid server token (%s); closing 1008", exc)
            await ws.close(code=1008, reason="invalid token")
            return

        # 2. Handshake: capability negotiation. We don't gate the link on
        #    this — daemon always supports rpc_v1 — but it gives the
        #    caller a single round-trip ack.
        await ws.send(json.dumps({"type": "heartbeat_ack", "rpc_v1": True}))

        # 3. Build the bidirectional link and start its read loop. The
        #    read loop runs until the socket drops; we then clear
        #    ``current_link`` so the claim loop falls back to HTTP.
        link = WsRpcLink(ws, on_request=self._handle_inbound_request)
        await link.start()
        async with self._link_lock:
            self.current_link = link

        peer = getattr(ws, "remote_address", None)
        log.info("DaemonWsServer: server dial accepted from %s", peer)

        try:
            if link._read_task is not None:  # noqa: SLF001
                # Block here until the read loop exits (i.e. disconnect).
                await link._read_task
            log.info("DaemonWsServer: link closed (peer %s)", peer)
        except (asyncio.CancelledError, Exception) as exc:
            log.info("DaemonWsServer: link terminated (%s): %s", peer, exc)
        finally:
            async with self._link_lock:
                if self.current_link is link:
                    self.current_link = None
            try:
                await link.close()
            except Exception:
                pass

    async def _handle_inbound_request(self, method: str, body: dict) -> dict:
        """Daemon-side inbound RPC handler. Currently returns unknown_method.

        Step 3.3 leaves the daemon as the requester only; the server
        responds. Future ``tasks.assign`` (server-push) would live here.
        """
        raise ValueError({"type": "unknown_method", "message": method})

    def _require_link(self) -> WsRpcLink:
        """Return ``current_link`` or raise ``WSRPCUnavailable`` (no connection)."""
        link = self.current_link
        if link is None or link.closed:
            raise WSRPCUnavailable("no link")
        return link


__all__ = ["DaemonWsServer"]