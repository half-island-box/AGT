"""Runtime catalog — the single source of truth for "which daemons are live".

Phase B (MCP): agent CLIs query a *live* view of the registry so a parent
agent can pick a valid ``daemon_id``/``provider`` before spawning a child.
The catalog has exactly two consumers, both funneling through
:func:`render_runtime_catalog` so they can never drift:

- the Server-side MCP tool (``list_runtimes``) — calls it in-process;
- the reverse-dial ``catalog.list`` inbound RPC — the Server answers a
  remote daemon's proxy MCP by running the same render.

Liveness: the catalog shows **only live daemons** (Phase A decision D3).
Runners are read through :func:`agt.server.services.daemon_service.list_daemons`
(which aggregates + overlays the static ``~/.agt/daemons`` url/capability
labels) and then filtered on their ``online`` flag. Optional ``capability``
and ``provider`` filters narrow the view for callers that already know
what they are looking for.
"""

from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.services.daemon_service import list_daemons


# Tool description shared by the Server-side MCP tool and the remote-daemon
# proxy MCP tool. Keeping it in one place means the two never drift.
CATALOG_TOOL_DESCRIPTION = (
    "List live daemons available for spawning sub-agents, with their "
    "providers, versions, and capability labels. Optional filters: "
    "'capability' (only daemons carrying that label) and 'provider' (only "
    "daemons that can run that provider). Results are JSON; each entry's "
    "'daemon_id' and 'provider' fields can be used to pin a spawn."
)


def _matches_capability(daemon: dict[str, Any], capability: str | None) -> bool:
    if capability is None:
        return True
    return capability in daemon.get("capabilities", [])


def _filter_providers(
    providers: list[dict[str, Any]], provider: str | None
) -> list[dict[str, Any]]:
    if provider is None:
        return providers
    return [p for p in providers if p.get("provider") == provider]


def _catalog_entry(daemon: dict[str, Any]) -> dict[str, Any]:
    """Project an aggregated daemon dict into the agent-facing catalog shape.

    Kept deliberately small: a parent agent needs the stable id (to pin a
    spawn), a human label, the static capability labels, and the list of
    providers that daemon can actually run (provider + version).
    """
    return {
        "daemon_id": daemon["daemon_id"],
        "name": daemon.get("daemon_name") or daemon["daemon_id"],
        "url": daemon.get("url"),
        "capabilities": list(daemon.get("capabilities", []) or []),
        "online": bool(daemon.get("online")),
        "providers": [
            {
                "provider": p["provider"],
                "name": p.get("name"),
                "version": p.get("version"),
                "status": p.get("status"),
            }
            for p in daemon.get("providers", [])
        ],
    }


async def render_runtime_catalog(
    db: AsyncSession,
    *,
    daemon_defs: Mapping[str, Any] | None = None,
    capability: str | None = None,
    provider: str | None = None,
) -> dict[str, Any]:
    """Return ``{"daemons": [...]}`` for every LIVE daemon, filtered.

    ``daemon_defs`` (``daemon_id → DaemonTarget``) supplies the static
    url/capability/display-name overlay; when None the catalog omits that
    metadata (the caller is a test or a server without a ``~/.agt/daemons``
    file).
    """
    daemons = await list_daemons(db, daemon_defs=daemon_defs)
    entries: list[dict[str, Any]] = []
    for daemon in daemons:
        if not daemon.get("online"):
            continue
        if not _matches_capability(daemon, capability):
            continue
        providers = _filter_providers(daemon.get("providers", []), provider)
        if provider is not None and not providers:
            continue
        entry = _catalog_entry(daemon)
        entry["providers"] = providers
        entries.append(entry)
    return {"daemons": entries}


__all__ = ["render_runtime_catalog", "CATALOG_TOOL_DESCRIPTION"]