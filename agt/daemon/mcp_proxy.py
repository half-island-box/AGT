"""Remote-daemon proxy MCP — forwards catalog queries over the reverse-dial link.

A remote agent (behind NAT) cannot reach the Server's HTTP MCP endpoint, so
its daemon runs a *proxy* MCP server on ``127.0.0.1:<mcp_port>`` (Streamable
HTTP). The proxy exposes the **same** ``list_runtimes`` tool as the Server,
but instead of rendering locally it forwards each call over the existing
reverse-dial WebSocket as a ``catalog.list`` inbound RPC
(:mod:`agt.server.daemon_link`). No catalog data lives on the daemon; this is
a pure forward — the "processing only remote daemons do".

When the reverse-dial link is down, the tool answers a graceful
``{"error": "catalog unavailable"}`` string instead of hanging, so the agent
is never left waiting on a dead link.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP

from ..comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain
from ..server.mcp_server import catalog_mcp_asgi
from ..server.services.catalog_service import CATALOG_TOOL_DESCRIPTION

log = logging.getLogger("agt.daemon.mcp_proxy")

# Same as the Server-side tool: binds to 127.0.0.1 only (never exposed), on
# a dedicated port so it can't collide with the reverse-dial WS listener.
DEFAULT_PROXY_MCP_PORT = 8428


async def list_runtimes_proxy_impl(
    ws_server: Any,
    capability: str | None = None,
    provider: str | None = None,
) -> str:
    """Forward one catalog query over the reverse-dial link.

    Factored out of the MCP tool so it is unit-testable without a live MCP
    transport or a real WebSocket.
    """
    link = getattr(ws_server, "current_link", None)
    if link is None or link.closed or not link.supports_rpc_v1:
        return json.dumps({"error": "catalog unavailable: no reverse-dial link"})
    try:
        result = await link.call(
            "catalog.list",
            {"capability": capability, "provider": provider},
            timeout_ms=8000,
        )
    except (WSRPCUnavailable, WSRPCUncertain, RPCRejected) as exc:
        log.debug("catalog.list over reverse-dial failed: %s", exc)
        return json.dumps({"error": "catalog unavailable"})
    return json.dumps(result)


def build_proxy_mcp(ws_server: Any) -> FastMCP:
    """Build the daemon's proxy MCP server (one tool: ``list_runtimes``)."""
    mcp = FastMCP("agt-catalog-proxy")

    @mcp.tool(description=CATALOG_TOOL_DESCRIPTION)
    async def list_runtimes(
        capability: str | None = None,
        provider: str | None = None,
    ) -> str:
        return await list_runtimes_proxy_impl(ws_server, capability, provider)

    return mcp


class ProxyMcpServer:
    """Serves the proxy MCP over Streamable HTTP on ``127.0.0.1:<port>``.

    Owned by the daemon's ``run_background``. Only started in reverse-dial
    mode (``listen_mode``) — a local daemon's agents reach the Server's
    ``/mcp`` directly, so there is nothing to proxy.
    """

    def __init__(self, ws_server: Any, port: int = DEFAULT_PROXY_MCP_PORT) -> None:
        self._ws_server = ws_server
        self._port = port
        self._mcp = build_proxy_mcp(ws_server)
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Bind 127.0.0.1:<port> and start serving in a background task."""
        config = uvicorn.Config(
            catalog_mcp_asgi(self._mcp),
            host="127.0.0.1",
            port=self._port,
            log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(
            self._server.serve(), name="proxy-mcp"
        )
        log.info("Proxy MCP listening on http://127.0.0.1:%d/mcp", self._port)

    async def stop(self) -> None:
        """Tear down the proxy MCP server."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                self._task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._task
        self._server = None
        self._task = None


__all__ = [
    "ProxyMcpServer",
    "build_proxy_mcp",
    "list_runtimes_proxy_impl",
    "DEFAULT_PROXY_MCP_PORT",
]