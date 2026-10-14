"""Lightweight pid-file helpers for the agt server CLI.

These live in their own module so the CLI can read/write pid files without
importing ``agt.server.main``, which builds the FastAPI app (and therefore
checks ``AGT_SERVER_SECRET``) at module load time.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path


# Default pid file location. Computed lazily so ``HOME`` overrides
# (set in tests / by users with non-default homes) take effect.
DEFAULT_PID_FILE = Path.home() / ".agt" / "server.pid"
# Kept as an alias for backward-compatible imports.
PID_FILE = DEFAULT_PID_FILE


def _default_pid_file() -> Path:
    """Return the canonical pid file path, resolving ``HOME`` at call time."""
    return Path.home() / ".agt" / "server.pid"


def _read_pidfile(path: Path | None = None) -> int | None:
    """Return the live pid stored in ``path``, or None if stale/missing.

    If ``path`` is None, falls back to the canonical ``~/.agt/server.pid``,
    computed lazily (respects runtime ``HOME``).
    """
    target = path if path is not None else _default_pid_file()
    if not target.exists():
        return None
    try:
        pid = int(target.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    # Probe liveness: kill(pid, 0) succeeds iff the process exists.
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except PermissionError:
        # Different user owns the process; treat as running but don't signal.
        return pid


def _signal_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    """Send ``sig`` to ``pid``. Raises on ESRCH (process already gone)."""
    os.kill(pid, sig)


__all__ = [
    "DEFAULT_PID_FILE",
    "PID_FILE",
    "_default_pid_file",
    "_read_pidfile",
    "_signal_pid",
]
