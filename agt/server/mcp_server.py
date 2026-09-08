"""Server-side MCP endpoint — exposes the live runtime catalog to agent CLIs.

Mounted onto the existing FastAPI app at ``/mcp`` (Streamable HTTP), so a
local agent (or a local daemon's agent) can query which daemons are live
before spawning a child. The tool is a thin adapter over
:func:`agt.server.services.catalog_service.render_runtime_catalog`; the
reverse-dial ``catalog.list`` RPC (in :mod:`agt.server.daemon_link`) runs
the *same* render for remote daemons, so the two paths can't drift.

Only the tool *logic* is kept importable/testable here
(:func:`list_runtimes_impl`); the MCP transport (``FastMCP`` + Streamable
HTTP) is a thin wrapper, so tests exercise the logic without a live MCP
connection.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import FastMCP

from .database import session_scope
from .services.catalog_service import (
    CATALOG_TOOL_DESCRIPTION,
    render_runtime_catalog,
)

log = logging.getLogger("agt.server.mcp")


async def list_runtimes_impl(
    session_factory: Any,
    daemon_defs: dict[str, Any],
    capability: str | None = None,
    provider: str | None = None,
) -> str:
    """Render the live catalog and serialize it to a JSON string.

    Factored out of the MCP tool so it is directly unit-testable without
    spinning up the MCP transport.
    """
    async with session_scope(session_factory) as db:
        catalog = await render_runtime_catalog(
            db,
            daemon_defs=daemon_defs,
            capability=capability,
            provider=provider,
        )
    return json.dumps(catalog)


def build_catalog_mcp(
    session_factory: Any,
    daemon_defs: dict[str, Any],
) -> FastMCP:
    """Build the Server's catalog MCP server (one tool: ``list_runtimes``)."""
    mcp = FastMCP("agt-catalog")

    @mcp.tool(description=CATALOG_TOOL_DESCRIPTION)
    async def list_runtimes(
        capability: str | None = None,
        provider: str | None = None,
    ) -> str:
        return await list_runtimes_impl(
            session_factory, daemon_defs, capability, provider,
        )

    return mcp


def catalog_mcp_asgi(mcp: FastMCP) -> Any:
    """Return the Streamable-HTTP ASGI app for ``mcp``.

    Tolerates method-name drift across SDK versions (``streamable_http_app``
    is the current name; older releases used ``http_app``). Raises a clear
    error only if neither exists, so a broken install fails loudly at
    import/app-build time instead of 500ing at runtime.
    """
    for name in ("streamable_http_app", "http_app"):
        fn = getattr(mcp, name, None)
        if callable(fn):
            return fn()
    raise AttributeError(
        "Installed 'mcp' SDK exposes neither streamable_http_app nor "
        "http_app — upgrade the 'mcp' package."
    )


__all__ = [
    "list_runtimes_impl",
    "build_catalog_mcp",
    "catalog_mcp_asgi",
]