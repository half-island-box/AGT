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

The daemon answers two inbound RPC requests: ``tasks.available`` (wakes the
claim loop) and ``session.history`` (reads a session transcript from this
machine's provider store, backing the CLI's remote ``--resume`` history
echo). Any other inbound ``rpc_request`` is answered with an
``unknown_method`` error so the caller doesn't hang on its pending future.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve

from ..agent_history import load_history
from ..comm.errors import WSRPCUnavailable
from ..comm.wsrpc_link import WsRpcLink
from ..server.services.auth_service import TaskToken

log = logging.getLogger("agt.daemon.ws")

# Hook called once per inbound server connection, after the link is
# exposed as ``current_link`` and ready to issue RPC. Used by the daemon
# to push its runtime snapshot immediately so the Server's registry is
# refreshed without waiting for the first 30s heartbeat. Failures are
# logged and swallowed — they must not abort the link.
OnConnectHook = Callable[[WsRpcLink], Awaitable[None]]


class DaemonWsServer:
    """Daemon-side WS server. Exposes ``current_link`` for the claim loop."""

    def __init__(
        self,
        port: int,
        secret: str,
        *,
        on_connect: OnConnectHook | None = None,
    ) -> None:
        self._port = port
        self._secret = secret
        self._server: serve | None = None
        self.current_link: WsRpcLink | None = None
        self._link_lock = asyncio.Lock()
        self._on_connect = on_connect
        # Woken by an inbound ``tasks.available`` notify from the Server.
        # The claim loop sleeps on this event with a timeout so idle
        # polling is interrupted as soon as new work arrives.
        self.claim_wakeup = asyncio.Event()

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

        # 4. Fire the on_connect hook (if any). This is where the daemon
        # pushes its runtime snapshot so the Server's registry reflects
        # the current probe immediately, instead of waiting up to 30s
        # for the next heartbeat. The hook is best-effort: any error is
        # logged and swallowed so a buggy hook can't poison the link.
        if self._on_connect is not None:
            try:
                await self._on_connect(link)
            except Exception as exc:  # pragma: no cover — defensive
                log.warning("DaemonWsServer on_connect hook failed: %s", exc)

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
        """Daemon-side inbound RPC handler.

        Handles ``tasks.available`` — a latency hint from the Server that
        task(s) may be claimable — and ``session.history``, which reads a
        session's transcript from this machine's provider store (the CLI's
        remote ``--resume`` history echo) — and ``artifacts.fetch`` (Phase
        2), the lazy pull of a declared deliverable's bytes. Other methods
        are unsupported.
        """
        if method == "tasks.available":
            self.wake_claim_loop()
            return {"ok": True}
        if method == "session.history":
            return await self._handle_session_history(body)
        if method == "artifacts.fetch":
            return await self._handle_artifacts_fetch(body)
        raise ValueError({"type": "unknown_method", "message": method})

    async def _handle_artifacts_fetch(self, body: dict) -> dict:
        """Serve one artifact's bytes from this daemon's local disk.

        The server relays back the ``local_path`` this daemon itself
        reported at registration time, so the path is trusted input from
        an HMAC-authenticated server. A missing/unreadable file answers
        ``artifact_missing`` so the server can mark the row ``lost``.
        """
        local_path = body.get("local_path")
        if not isinstance(local_path, str) or not local_path:
            raise ValueError({
                "type": "validation_error",
                "message": "artifacts.fetch requires local_path",
            })
        import base64
        import hashlib
        from pathlib import Path

        path = Path(local_path)
        try:
            content = await asyncio.to_thread(path.read_bytes)
        except OSError:
            raise ValueError({
                "type": "artifact_missing",
                "reason": "artifact_missing",
                "message": f"local file gone: {local_path}",
            }) from None
        expected = body.get("sha256")
        if isinstance(expected, str) and expected:
            digest = hashlib.sha256(content).hexdigest()
            if digest != expected:
                raise ValueError({
                    "type": "artifact_hash_mismatch",
                    "reason": "artifact_missing",
                    "message": f"sha256 mismatch: {digest}",
                })
        return {"content_b64": base64.b64encode(content).decode()}

    async def _handle_session_history(self, body: dict) -> dict:
        """Read a session transcript from this daemon's local provider store.

        ``load_history`` walks ``~/.claude`` / ``~/.codex`` — the home of
        the daemon process, which is where the agent's transcripts live. It
        is read-only and best-effort: a missing file yields an empty list,
        and a provider with no reader yields ``not_implemented=True`` so the
        CLI can show the same hint it does for local resumes.
        """
        provider = body.get("provider")
        session_id = body.get("session_id")
        if not isinstance(provider, str) or not isinstance(session_id, str):
            raise ValueError({
                "type": "validation_error",
                "message": "session.history requires provider + session_id",
            })
        # Run in a worker thread: the codex reader ``rglob``s the sessions
        # tree, which can block the event loop for a beat on a busy daemon.
        try:
            messages = await asyncio.to_thread(load_history, provider, session_id)
        except NotImplementedError as exc:
            return {"messages": [], "not_implemented": True, "reason": str(exc)}
        return {
            "messages": [
                {"role": m.role, "content": m.content, "timestamp": m.timestamp}
                for m in messages
            ],
            "not_implemented": False,
        }

    def wake_claim_loop(self) -> None:
        """Wake the daemon's claim loop immediately.

        Safe to call from any thread/task; if the loop is already awake
        the event remains set, which causes the next sleep to return
        immediately.
        """
        self.claim_wakeup.set()

    def _require_link(self) -> WsRpcLink:
        """Return ``current_link`` or raise ``WSRPCUnavailable`` (no connection)."""
        link = self.current_link
        if link is None or link.closed:
            raise WSRPCUnavailable("no link")
        return link


__all__ = ["DaemonWsServer"]
