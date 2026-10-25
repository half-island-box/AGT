"""Server-side MCP endpoint for runtime discovery and Goal child spawning.

Mounted onto the existing FastAPI app at ``/mcp`` (Streamable HTTP), so a
local agent (or a local daemon's agent) can query the live runtime catalog
and spawn children in Goal mode. The spawn tool authenticates the caller
with the task-scoped bearer token injected into the agent environment.

Only the tool logic is kept importable/testable here; the MCP transport
(``FastMCP`` + Streamable HTTP) is a thin wrapper.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from .database import session_scope
from .services.auth_service import bearer_token_from_header
from .services.catalog_service import (
    CATALOG_TOOL_DESCRIPTION,
    render_runtime_catalog,
)
from .services.spawn_service import (
    SPAWN_TOOL_DESCRIPTION,
    SpawnChildInput,
    spawn_children_for_token,
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


async def spawn_subagents_impl(
    session_factory: Any,
    secret: str,
    authorization: str | None,
    children: list[dict],
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Authenticate and create Goal child Jobs through the MCP surface."""
    token = bearer_token_from_header(authorization)
    try:
        async with session_scope(session_factory) as db:
            return await spawn_children_for_token(
                db,
                task_token=token,
                secret=secret,
                children=children,
                idempotency_key=idempotency_key,
            )
    except ValueError as exc:
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        code = payload.get("code") or payload.get("type") or "spawn_failed"
        return {
            "ok": False,
            "error": {
                "code": code,
                "message": payload.get("message", str(exc)),
            },
        }


def build_catalog_mcp(
    session_factory: Any,
    daemon_defs: dict[str, Any],
    secret: str = "",
) -> FastMCP:
    """Build the Server MCP app (catalog + Goal spawn tools).

    ``streamable_http_path="/"`` is deliberate: the app is mounted at
    ``/mcp`` on the FastAPI app (``app.mount("/mcp", ...)``), so the
    FastMCP default (``/mcp``) would stack with the mount prefix into a
    double ``/mcp/mcp``. Pin the sub-app route to ``/`` so the assembled
    endpoint is the single ``/mcp/`` the executor injects.
    """
    mcp = FastMCP("agt-catalog", streamable_http_path="/")

    @mcp.tool(description=CATALOG_TOOL_DESCRIPTION)
    async def list_runtimes(
        capability: str | None = None,
        provider: str | None = None,
    ) -> str:
        return await list_runtimes_impl(
            session_factory, daemon_defs, capability, provider,
        )

    @mcp.tool(description=SPAWN_TOOL_DESCRIPTION)
    async def spawn_subagents(
        ctx: Context,
        children: list[SpawnChildInput],
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        request = ctx.request_context.request
        authorization = (
            request.headers.get("authorization") if request is not None else None
        )
        return await spawn_subagents_impl(
            session_factory,
            secret,
            authorization,
            [child.model_dump(exclude_none=True, by_alias=True) for child in children],
            idempotency_key,
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
    "spawn_subagents_impl",
    "build_catalog_mcp",
    "catalog_mcp_asgi",
]
