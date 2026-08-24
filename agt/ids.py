"""Shared deterministic identifiers.

Single source of truth for the daemon-id namespace so the daemon and
the server independently derive the SAME id from a daemon URL. The
``~/.agt/daemons`` file only carries ``name url`` — the id is always
computed (uuid5 in this namespace), never stored in the file. That
keeps both sides in agreement without any side channel:

    daemon side:  DaemonConfig.from_env() → _load_daemon_id(url)
    server side:  _parse_daemons_file()   → DaemonTarget.daemon_id
    CLI side:     _local_daemon_id()

All three call :func:`daemon_id_from_url` on the same URL string and
therefore agree by construction.
"""

from __future__ import annotations

import uuid

# Fixed namespace so ``uuid5(NS, url)`` is deterministic across machines
# and restarts: the same daemon URL always yields the same daemon_id.
AGT_DAEMON_NAMESPACE = uuid.UUID("6f9c2a41-0b7e-4d3c-9f1a-2c3d4e5f6a7b")


def daemon_id_from_url(url: str) -> str:
    """Deterministic daemon id for a daemon URL (uuid5, shared namespace)."""
    return str(uuid.uuid5(AGT_DAEMON_NAMESPACE, url))


__all__ = ["AGT_DAEMON_NAMESPACE", "daemon_id_from_url"]
