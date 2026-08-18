"""DaemonLink — server-side per-daemon reverse-dial connection (Step 3.5).

Each ``DaemonLink`` corresponds to one daemon in the Server's static
``~/.agt/daemons.yaml`` config. The link owns:

- The TCP/WS dial (Server-initiated; daemon listens on its public port).
- The HMAC-signed ``X-Server-Token`` so the daemon accepts the dial.
- A :class:`WsRpcLink` that multiplexes RPC in both directions.
- An ``on_request`` handler that responds to ``tasks.claim`` frames sent
  by the daemon over this connection.
- A reconnect loop with exponential backoff (1s → 60s cap) when the
  dial fails or the connection drops.

The daemon's ``daemon_id`` is taken from the configured target — never
from the peer's body — so a misbehaving daemon cannot claim work for
some other daemon.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import websockets

from ..comm.errors import RPCRejected, WSRPCUnavailable
from ..comm.wsrpc_link import WsRpcLink
from .database import session_scope
from .services.auth_service import TaskToken
from .services.claim_service import claim_tasks_for_daemon
from .services.report_service import record_task_result_for_daemon
from .services.runtime_service import upsert_daemon_runtimes

log = logging.getLogger("agt.server.daemon_link")

# Reconnect backoff bounds.
_BACKOFF_INITIAL = 1.0
_BACKOFF_CAP = 60.0


class DaemonLink:
    """Server-side persistent connection to one remote daemon."""

    def __init__(
        self,
        *,
        daemon_id: str,
        url: str,
        secret: str,
        session_factory: Any,
    ) -> None:
        self.daemon_id = daemon_id
        self.url = url
        self._secret = secret
        self._session_factory = session_factory

        self._link: WsRpcLink | None = None
        self._active = False  # True once a connection has been accepted at least once

    @property
    def connected(self) -> bool:
        return self._link is not None and not self._link.closed

    async def run(self, shutdown: asyncio.Event) -> None:
        """Dial + read-loop + reconnect. Runs until ``shutdown`` is set.

        Logging policy: we only log on **state changes** (first connect,
        reconnect, disconnect) — not on every retry attempt. Otherwise a
        unreachable daemon would spam the log every 1s/2s/4s/... up to
        60s as the backoff doubles.
        """
        backoff = _BACKOFF_INITIAL
        ever_connected = False
        while not shutdown.is_set():
            try:
                await self._connect_and_serve()
                backoff = _BACKOFF_INITIAL
                if not ever_connected:
                    log.info(
                        "DaemonLink %s connected (%s)", self.daemon_id, self.url,
                    )
                    ever_connected = True
                else:
                    log.info(
                        "DaemonLink %s reconnected (%s)", self.daemon_id, self.url,
                    )
            except Exception as exc:
                if shutdown.is_set():
                    break
                if not ever_connected:
                    # Pre-first-connect: state is "trying to reach"; quiet.
                    log.debug(
                        "DaemonLink %s not yet reachable: %s (retry in %.1fs)",
                        self.daemon_id, exc, backoff,
                    )
                else:
                    # State transition: was connected, now isn't. Log once.
                    log.warning(
                        "DaemonLink %s disconnected: %s — retrying in %.1fs",
                        self.daemon_id, exc, backoff,
                    )
                    ever_connected = False  # next connect is a "reconnect"
            if shutdown.is_set():
                break
            # Sleep with cancellation support.
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, _BACKOFF_CAP)

    async def _connect_and_serve(self) -> None:
        """One connect-and-serve cycle. Raises on disconnect/dial failure."""
        ws = await websockets.connect(
            f"{self.url}/daemon",
            additional_headers={
                "X-Server-Token": TaskToken.create_server("server", self._secret),
            },
            ping_interval=20,
            ping_timeout=20,
            open_timeout=10,
        )
        # Server → daemon capability negotiation. Without this, the
        # daemon's own ``WsRpcLink`` never receives a ``heartbeat_ack``
        # (the daemon *sends* one to us, but that marks *our* link as
        # rpc_v1-capable, not theirs), so its ``supports_rpc_v1`` stays
        # False and ``claim_tasks_ws_first`` falls back to HTTP — which
        # is unreachable in a reverse-dial setup. Send the ack before
        # spinning up the read loop.
        await ws.send(json.dumps({"type": "heartbeat_ack", "rpc_v1": True}))
        # Pass on_request so we respond to inbound claim frames.
        link = WsRpcLink(ws, on_request=self._handle_inbound_request)
        await link.start()
        self._link = link
        self._active = True
        try:
            # Block until the read loop exits (i.e., socket dropped).
            if link._read_task is not None:  # noqa: SLF001
                await link._read_task
        finally:
            try:
                await link.close()
            except Exception:
                pass
            self._link = None

    async def _handle_inbound_request(self, method: str, body: dict) -> dict:
        """Respond to RPC requests the daemon sends over this connection."""
        if method == "tasks.claim":
            return await self._handle_claim(body)
        if method == "runtimes.register":
            return await self._handle_runtimes_register(body)
        if method == "tasks.report":
            return await self._handle_tasks_report(body)
        raise ValueError({"type": "unknown_method", "message": method})

    async def _handle_claim(self, body: dict) -> dict:
        # Body may include runtime_ids (provider filter from Step 3.4) and
        # max_tasks; ``daemon_id`` in the body is **ignored** — we use
        # the configured ``self.daemon_id`` so a daemon can't claim on
        # behalf of another.
        runtime_ids = body.get("runtime_ids") or []
        if not isinstance(runtime_ids, list):
            raise ValueError({"type": "validation_error", "message": "runtime_ids must be a list"})
        try:
            max_tasks = int(body.get("max_tasks", 1) or 1)
        except (TypeError, ValueError):
            raise ValueError({"type": "validation_error", "message": "max_tasks must be an integer"})
        if max_tasks < 1:
            max_tasks = 1

        async with session_scope(self._session_factory) as db:
            tasks = await claim_tasks_for_daemon(
                db,
                daemon_id=self.daemon_id,
                max_tasks=max_tasks,
                timeout_seconds=0,  # WS claim is non-blocking; long-poll is the caller's job
                runtime_ids=runtime_ids,
            )
        return {"tasks": [t.model_dump() for t in tasks]}

    async def _handle_runtimes_register(self, body: dict) -> dict:
        """Sync the daemon's runtime snapshot into the registry.

        ``daemon_id`` comes from the configured target, never the body.
        """
        runtimes = body.get("runtimes")
        if not isinstance(runtimes, list):
            raise ValueError({"type": "validation_error", "message": "runtimes must be a list"})
        async with session_scope(self._session_factory) as db:
            count = await upsert_daemon_runtimes(
                db,
                daemon_id=self.daemon_id,
                runtimes=runtimes,
            )
        return {"registered": count}

    async def _handle_tasks_report(self, body: dict) -> dict:
        """Record a daemon's task execution result over the reverse-dial link.

        ``daemon_id`` comes from the configured target, never the body —
        a misbehaving daemon can't report on behalf of another. This is
        the reverse-dial counterpart to ``POST /api/tasks/{id}/report``;
        both paths funnel through :func:`record_task_result_for_daemon`.

        Business rejections (task missing / not owned) surface as
        ``RPCRejected(404|403)`` so the daemon can tell the difference
        between a permanent failure (don't retry) and a transport issue
        (do retry via HTTP fallback). Other ``ValueError`` payloads fall
        through to the link's generic ``validation_error`` shape.
        """
        required = (
            "task_id", "exit_code", "stdout", "stderr", "duration", "status",
        )
        for field_name in required:
            if field_name not in body:
                raise ValueError({
                    "type": "validation_error",
                    "message": f"missing field: {field_name}",
                })
        try:
            async with session_scope(self._session_factory) as db:
                return await record_task_result_for_daemon(
                    db,
                    daemon_id=self.daemon_id,
                    task_id=body["task_id"],
                    exit_code=int(body["exit_code"]),
                    stdout=str(body["stdout"]),
                    stderr=str(body["stderr"]),
                    duration=float(body["duration"]),
                    status=str(body["status"]),
                    session_id=body.get("session_id"),
                    resume_rejected=bool(body.get("resume_rejected", False)),
                )
        except ValueError as exc:
            payload = (
                exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
            )
            err_type = payload.get("type")
            if err_type == "task_not_found":
                raise RPCRejected(404, payload) from None
            if err_type == "task_not_owned":
                raise RPCRejected(403, payload) from None
            # Unknown ValueError shape — let wsrpc_link turn it into
            # a generic validation_error response.
            raise


__all__ = ["DaemonLink"]