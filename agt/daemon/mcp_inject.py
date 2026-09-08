"""MCP config injection + reclaim for daemon spawn (Phase B).

When a daemon spawns an agent, that agent may want to query the live
runtime catalog (which daemons are up, what they can run) before spawning
children. The catalog is served over MCP:

- **Local agents** reach the Server's Streamable-HTTP MCP endpoint
  (``<server_url>/mcp``) directly.
- **Remote agents** (behind NAT) reach their daemon's proxy MCP at
  ``127.0.0.1:<mcp_port>/mcp`` — see :mod:`agt.daemon.mcp_proxy`.

To make the agent *discover* that MCP server we inject a provider-specific
config file into the spawn cwd, then reclaim it when the spawn finishes.
This mirrors the inject/reclaim pattern of :mod:`agt.daemon.identity_inject`
but is deliberately simpler: MCP config is a whole file (not a marker block
inside a shared instructions file), so we back up the original bytes and
restore them on reclaim.

Supported providers (this is a first cut — the mapping is the extension
point, not the mechanism):

- ``claude`` → project-scoped ``.mcp.json`` (``{"mcpServers": {...}}``).
  Claude Code reads this from the project root (its spawn cwd). The
  remote-HTTP entry shape is ``{"type": "http", "url": ...}``.

  Codex (``~/.codex/config.toml``) is deliberately **not** supported yet:
  that file is *global*, not project-scoped, so the "write into cwd +
  restore on reclaim" contract does not apply — reclaiming a global file
  safely across concurrent sessions is a different design and would need
  its own locking. Unknown/other providers are a no-op for now.

Design notes:

- **Non-destructive**: a pre-existing ``.mcp.json`` is never overwritten
  out from under the user — its original bytes are saved to a sidecar in
  ``.agt/`` and restored on reclaim. Only a file we created ourselves
  (tracked by a "managed" sidecar) is deleted on reclaim.
- **Idempotent / crash-self-heal**: inject reclaims first, so a leftover
  from a crashed prior run is cleaned up before re-injecting.
- **Pure file IO**: no DB, no daemon state. Reclaim is a pure function of
  cwd contents and must succeed (or no-op) even after a daemon restart.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path

# The MCP server name an agent references when it calls the catalog tool.
MCP_SERVER_NAME = "agt-catalog"

# Provider → project-scoped MCP config filename. Only ``claude`` is mapped
# for now; see the module docstring for why Codex and others are absent.
_MCP_CONFIG_FILENAMES: dict[str, str] = {
    "claude": ".mcp.json",
}

# Sidecar names under ``<cwd>/.agt/``.
#  - ``managed`` records "we created the file this time" → reclaim may
#    delete it.
#  - ``backup`` holds the original bytes when the file pre-existed →
#    reclaim restores them.
_SIDECAR_PREFIX = ".agt-mcp"


def _mcp_config_filename(provider: str | None) -> str | None:
    """The project-scoped MCP config file ``provider`` reads, or None.

    ``None`` means "no project-scoped MCP config mechanism" — the agent
    simply won't have the catalog tool (accepted degradation), and both
    inject and reclaim become no-ops.
    """
    if not provider:
        return None
    return _MCP_CONFIG_FILENAMES.get(provider)


def _sidecars(cwd: Path, filename: str) -> tuple[Path, Path]:
    """Return the ``(backup, managed)`` sidecar paths for ``filename``."""
    aagt = cwd / ".agt"
    return (
        aagt / f"{_SIDECAR_PREFIX}-backup-{filename}",
        aagt / f"{_SIDECAR_PREFIX}-managed-{filename}",
    )


def inject_mcp_config(cwd: Path, provider: str | None, mcp_url: str) -> None:
    """Register ``agt-catalog`` MCP in the provider's project config.

    Writes the merged config into ``cwd``'s provider config file (for
    ``claude``, ``.mcp.json``). Idempotent (reclaim-first). A no-op for
    providers without a project-scoped MCP config file.

    ``mcp_url`` is the Streamable-HTTP endpoint the agent should call;
    the caller (executor) picks it from daemon config (local → Server
    ``/mcp``, remote → ``127.0.0.1:<mcp_port>/mcp``).
    """
    filename = _mcp_config_filename(provider)
    if filename is None:
        return
    cwd = Path(cwd)
    cwd.mkdir(parents=True, exist_ok=True)

    # Crash-self-heal: strip a leftover from a prior run before re-injecting.
    reclaim_mcp_config(cwd, provider)

    target = cwd / filename
    aagt = cwd / ".agt"
    aagt.mkdir(parents=True, exist_ok=True)
    backup, managed = _sidecars(cwd, filename)

    # Read any pre-existing file, tolerating a TOCTOU race (the file
    # vanishes between ``exists()`` and the read). ``raw is None`` means
    # "no original to preserve" — either the file never existed or it
    # disappeared mid-read, and we treat it as ours.
    original: dict = {}
    raw: bytes | None = None
    try:
        if target.exists():
            raw = target.read_bytes()
    except OSError:
        raw = None

    if raw is not None:
        # Preserve the user's bytes verbatim; merge on top of a parsed copy.
        backup.write_bytes(raw)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            original = parsed
    else:
        managed.touch()

    servers = dict(original.get("mcpServers") or {})
    servers[MCP_SERVER_NAME] = {"type": "http", "url": mcp_url}
    merged = dict(original)
    merged["mcpServers"] = servers
    target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


def reclaim_mcp_config(cwd: Path, provider: str | None) -> None:
    """Undo a prior :func:`inject_mcp_config`. Idempotent.

    - If we created the file (``managed`` sidecar present) → delete it.
    - Else if a backup exists (the file pre-existed) → restore the
      original bytes.
    - Otherwise → nothing was injected, nothing to do.
    """
    filename = _mcp_config_filename(provider)
    if filename is None:
        return
    cwd = Path(cwd)
    target = cwd / filename
    backup, managed = _sidecars(cwd, filename)

    if managed.exists():
        with contextlib.suppress(OSError):
            target.unlink()
        with contextlib.suppress(OSError):
            managed.unlink()
        with contextlib.suppress(OSError):
            backup.unlink()
        return

    if backup.exists():
        try:
            target.write_bytes(backup.read_bytes())
        except OSError:
            pass
        with contextlib.suppress(OSError):
            backup.unlink()


__all__ = [
    "MCP_SERVER_NAME",
    "inject_mcp_config",
    "reclaim_mcp_config",
]
