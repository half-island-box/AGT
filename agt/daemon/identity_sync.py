"""Identity mirror sync for the daemon.

The daemon lives on a different machine than where ``agt identity create``
wrote the original ``~/.agt/identities/<slug>/AGENTS.md``. The original
inject path (``_inject_identity``) reads that file from ``Path.home()``,
so on a remote daemon the source is missing and the inject silently
no-ops. This module fixes that by ensuring the AGENTS.md mirror is present
and current before any inject runs.

Mechanism:

- The Server is the source of truth (``IdentityModel.updated_at`` is the
  freshness signal). Every ``GET /api/identities/{did}`` returns it.
- The daemon keeps a tiny ``~/.agt/daemon-identity-cache.json`` recording
  which ``updated_at`` it last saw for each did. Cache hit ⇒ skip render.
- On a mismatch (or first-time), the daemon fetches the full identity
  JSON, renders it to AGENTS.md via ``identity_render``, writes it under
  ``~/.agt/identities/<slug>/``, and updates the cache.
- All filesystem writes go through ``identity_inject``'s existing layout
  so nothing downstream needs to know about the sync.

Public surface:

- ``ensure_identity_md(server_url, server_secret, identity_did) -> Path | None``
- ``state_file_path() -> Path`` — exposed for tests / ops tools
- ``load_state()``, ``save_state()`` — exposed for tests
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from .identity_render import local_agent_md_path, render_agent_md

log = logging.getLogger(__name__)


# File name of the per-daemon cache. Lives at ``~/.agt/`` root level
# alongside ``daemons``, ``daemon.pid``, etc.
_STATE_FILENAME = "daemon-identity-cache.json"
_STATE_VERSION = 1
_FETCH_TIMEOUT_S = 5.0


def state_file_path() -> Path:
    """The cache file path for this daemon (``~/.agt/daemon-identity-cache.json``)."""
    return Path.home() / ".agt" / _STATE_FILENAME


def load_state() -> dict[str, Any]:
    """Read the cache from disk. Returns an empty cache on any error.

    A missing file is normal (first run on a fresh machine). A corrupt
    file is treated as empty — we never want to refuse injecting an
    identity just because the cache file got mangled; the next fetch
    will rebuild it.
    """
    path = state_file_path()
    if not path.exists():
        return {"version": _STATE_VERSION, "identities": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("identity cache %s unreadable (%s); treating as empty", path, exc)
        return {"version": _STATE_VERSION, "identities": {}}
    if not isinstance(data, dict) or "identities" not in data:
        return {"version": _STATE_VERSION, "identities": {}}
    identities = data.get("identities")
    if not isinstance(identities, dict):
        data["identities"] = {}
    return data


def save_state(state: dict[str, Any]) -> None:
    """Persist the cache atomically (tmp file + rename).

    Atomic write means two concurrent ``ensure_identity_md`` calls can't
    leave a half-written file — last writer wins, both write the same
    content (same ``updated_at``), so no harm done.
    """
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{_STATE_FILENAME}.", suffix=".tmp", dir=path.parent,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


async def _fetch_identity(
    *, server_url: str, server_secret: str, identity_did: str,
) -> dict[str, Any]:
    """GET /api/identities/{did} from the Server.

    Raises ``httpx.HTTPError`` on transport / non-2xx failures. Caller
    decides whether to fall back to the local mirror or surface an error.
    """
    headers = {"Accept": "application/json"}
    if server_secret:
        headers["X-Server-Secret"] = server_secret
    url = f"{server_url.rstrip('/')}/api/identities/{identity_did}"
    async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return resp.json()


async def ensure_identity_md(
    *,
    server_url: str,
    server_secret: str,
    identity_did: str,
) -> Path | None:
    """Ensure ``~/.agt/identities/<slug>/AGENTS.md`` is present and current.

    Returns the local AGENTS.md path on success, ``None`` on failure
    (network error AND no local file to fall back on — caller should
    treat as "no identity available", same as the pre-fix silent skip).

    Per-session cost: 1 RTT on a cache miss / staleness, 0 RTT on a hit.
    The cache is keyed on ``IdentityModel.updated_at`` so any change to
    the identity (description, capabilities, status, etc.) invalidates
    it on the next call.
    """
    target = local_agent_md_path(identity_did)

    # Fetch first — we always need the server's view to compare against.
    # The response body is small (~few hundred bytes) so we don't bother
    # with a HEAD / metadata-only path.
    try:
        data = await _fetch_identity(
            server_url=server_url,
            server_secret=server_secret,
            identity_did=identity_did,
        )
    except Exception as exc:
        log.warning(
            "identity %s: fetch from server failed (%s); falling back to "
            "local file if present",
            identity_did, exc,
        )
        return target if target.exists() else None

    server_updated_at = data.get("updated_at", "")
    state = load_state()
    identities = state.setdefault("identities", {})
    cached_updated_at = identities.get(identity_did)

    # Fast path: cache hit AND file present → nothing to do.
    if (
        cached_updated_at is not None
        and cached_updated_at == server_updated_at
        and target.exists()
    ):
        return target

    # Slow path: render and write.
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(render_agent_md(data), encoding="utf-8")
    except OSError as exc:
        log.warning(
            "identity %s: write to %s failed (%s)",
            identity_did, target, exc,
        )
        return target if target.exists() else None

    identities[identity_did] = server_updated_at
    try:
        save_state(state)
    except OSError as exc:
        # Worst case: the file is current on disk but the cache says
        # otherwise. Next call will just re-render unnecessarily.
        log.warning("identity %s: cache write failed (%s)", identity_did, exc)

    return target


__all__ = [
    "ensure_identity_md",
    "state_file_path",
    "load_state",
    "save_state",
]
