"""In-memory running-task registry for the daemon (Phase 4 cancel).

The daemon historically had no index of "what am I executing right
now" — steps were fire-and-forget asyncio tasks. Cancellation needs
that index: ``tasks.cancel`` (reverse-dial) and the claim-channel
``cancelled_task_ids`` both resolve task ids through this registry.

Deliberately memory-only and per-process: a daemon restart clears it,
which is correct — the subprocesses died with it, and the server's
late-report guard plus re-queue semantics handle the rest. Unknown ids
are always idempotent no-ops.
"""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

_REGISTRY: dict[str, asyncio.Task] = {}


def register(task_id: str) -> None:
    """Index the currently-running asyncio task under ``task_id``."""
    current = asyncio.current_task()
    if current is not None:
        _REGISTRY[task_id] = current


def unregister(task_id: str) -> None:
    """Remove ``task_id`` from the registry (any exit path)."""
    _REGISTRY.pop(task_id, None)


def is_running(task_id: str) -> bool:
    return task_id in _REGISTRY


def cancel(task_id: str) -> bool:
    """Cancel the asyncio task registered under ``task_id``.

    Returns True when a live task was cancelled; False for unknown or
    already-finished ids (idempotent no-op by design — the executor
    converts the cancellation into a ``cancelled`` report, which also
    drains the server's unreported-cancellation list).
    """
    task = _REGISTRY.get(task_id)
    if task is None or task.done():
        return False
    task.cancel()
    return True


def running_ids() -> list[str]:
    return [tid for tid, t in _REGISTRY.items() if not t.done()]


__all__ = ["register", "unregister", "is_running", "cancel", "running_ids"]
