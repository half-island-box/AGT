"""Shared URL-host locality helpers.

The daemon uses these to recognize its own entry in ``~/.agt/daemons``;
the Server uses the same check when a daemon target refers to this
machine. Keeping one implementation avoids the two sides disagreeing about
what ``local`` means.
"""

from __future__ import annotations

import socket
from urllib.parse import urlsplit


def host_of_url(url: str) -> str:
    """Extract the host from a ``ws://host:port`` / ``http://host:port`` URL."""
    try:
        return urlsplit(url).hostname or ""
    except ValueError:
        return ""


def is_local_host(host: str) -> bool:
    """True if ``host`` refers to this machine.

    Literal wildcard/loopback names always count. Otherwise the host is
    compared against the addresses this machine's hostname resolves to
    (local resolver — no external DNS, so this stays fast and never matches
    a public IP bound elsewhere).
    """
    if host in ("0.0.0.0", "127.0.0.1", "::", "localhost"):
        return True
    try:
        infos = socket.getaddrinfo(
            socket.gethostname(), None, socket.AF_UNSPEC, socket.SOCK_STREAM
        )
        local_ips = {a[4][0] for a in infos}
    except OSError:
        local_ips = set()
    return host.strip("[]") in local_ips


__all__ = ["host_of_url", "is_local_host"]
