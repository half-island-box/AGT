"""Lightweight PID-file helpers for the agt daemon CLI.

These live in their own module so the CLI can read the local Daemon's pid
file without importing ``agt.daemon.main``, which builds the daemon runtime.
"""

from __future__ import annotations

import os
from pathlib import Path


# Default pid file location. Computed lazily so ``HOME`` overrides
# (set in tests / by users with non-default homes) take effect.
default_pid_file = Path.home() / ".agt" / "daemon.pid"


def _default_pid_file() -> Path:
    """Return the canonical daemon pid file path, resolving ``HOME`` at call time."""
    return Path.home() / ".agt" / "daemon.pid"


def _pidfile_alive(pid: int) -> bool:
    """Return True if a process with this pid is alive AND belongs to a daemon."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # Best-effort: check /proc for the command line; tolerate failure on non-Linux.
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
        return "agt" in cmdline and "daemon" in cmdline
    except OSError:
        return True  # can't verify, assume alive


def read_daemon_pidfile(path: Path | None = None) -> int | None:
    """Return the live pid stored in ``path``, or None if stale/missing.

    If ``path`` is None, falls back to the canonical ``~/.agt/daemon.pid``,
    computed lazily (respects runtime ``HOME``).
    """
    target = path if path is not None else _default_pid_file()
    if not target.exists():
        return None
    try:
        pid = int(target.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    return pid if _pidfile_alive(pid) else None


__all__ = [
    "default_pid_file",
    "_default_pid_file",
    "_pidfile_alive",
    "read_daemon_pidfile",
]
