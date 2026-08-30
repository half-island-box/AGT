"""StreamHub — in-process pub/sub bridge between task writes and SSE streams.

Phase 5 (remote streaming): the server pushes task progress to CLI
subscribers over SSE. The producer side (``record_task_progress_for_daemon``
and ``record_task_result_for_daemon``) publishes events after committing to
the DB; the consumer side (``GET /api/tasks/{id}/stream``) subscribes and
drains a bounded per-subscriber queue.

Why a hub instead of the SSE endpoint polling the DB:
    Polling every task at 1s would not feel "streaming" and would multiply
    SQLite reads. A hub delivers each progress tick to active subscribers
    as soon as it is committed.

Correctness note (see design decision 5): correctness never depends on
this hub. ``snapshot`` and ``done`` are read from the DB, so even if a
delta is dropped here (slow consumer, process restart, multi-worker), the
client still converges to the exact ``TaskModel.stdout``. The hub only
buys *liveness* — the per-tick deltas that make it feel streaming.

The hub is process-local and therefore requires a **single-worker** server
for live deltas. Under multiple uvicorn workers the reverse-dial WS write
and the CLI's SSE read may land on different processes, silently degrading
to snapshot+done batch echo (still byte-correct).
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from typing import Any

# Cap on the "already terminal" marker map. It exists only to close the
# subscribe-vs-report race within one process lifetime; a task whose done
# event fell out of this cache is simply served via the endpoint's own
# terminal check (snapshot read straight from the DB).
_MAX_DONE_CACHE = 1024


class Subscriber:
    """One SSE connection's view of a task stream.

    ``cursor`` is the byte offset into the task's cumulative stdout that
    this subscriber has already been handed. It is per-subscriber (not
    per-task) so a fresh subscriber always aligns with the DB snapshot at
    subscribe time — a server restart therefore can never re-send already
    printed content (see design decision 3).
    """

    def __init__(self, task_id: str, *, cursor: int, maxsize: int) -> None:
        self.task_id = task_id
        self.cursor = cursor
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=maxsize
        )


class StreamHub:
    """In-process pub/sub over per-task subscriber sets.

    Events are plain dicts:

    - ``{"type": "progress", "stdout_prefix": str}`` — producer signals a
      growing stdout prefix. ``publish`` computes the per-subscriber chunk
      (``full_prefix[sub.cursor:]``), advances each subscriber's cursor, and
      enqueues ``{"type": "delta", "chunk": ...}`` when the chunk is
      non-empty.
    - ``{"type": "done", ...}`` — terminal report payload (full stdout,
      status, session_id, exit_code, resume_rejected, stderr). Enqueued
      verbatim to every subscriber and remembered so a subscriber that
      arrives after the report still gets its ``done``.
    """

    def __init__(self, *, maxsize: int = 256) -> None:
        self._subscribers: dict[str, set[Subscriber]] = {}
        self._done_events: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._maxsize = maxsize

    def subscribe(self, task_id: str, *, cursor: int = 0) -> Subscriber:
        """Register a subscriber and replay ``done`` if the task already
        finished (closes the subscribe-vs-report race)."""
        sub = Subscriber(task_id, cursor=cursor, maxsize=self._maxsize)
        self._subscribers.setdefault(task_id, set()).add(sub)
        done = self._done_events.get(task_id)
        if done is not None:
            # Deliver immediately; the subscriber's generator drains it
            # right after yielding its snapshot.
            try:
                sub.queue.put_nowait(done)
            except asyncio.QueueFull:
                self.unsubscribe(task_id, sub)
        return sub

    def unsubscribe(self, task_id: str, sub: Subscriber) -> None:
        subs = self._subscribers.get(task_id)
        if subs is None:
            return
        subs.discard(sub)
        if not subs:
            self._subscribers.pop(task_id, None)

    def publish(self, task_id: str, event: dict[str, Any]) -> None:
        """Fan ``event`` out to every subscriber of ``task_id``.

        Never blocks: bounded queues + ``put_nowait`` mean a slow consumer
        is dropped (its client reconnects and re-syncs via snapshot) rather
        than stalling the report/progress write path.
        """
        etype = event.get("type")
        if etype == "done":
            self._remember_done(task_id, event)

        for sub in list(self._subscribers.get(task_id, ())):
            try:
                if etype == "progress":
                    full_prefix = event.get("stdout_prefix") or ""
                    chunk = full_prefix[sub.cursor:]
                    if not chunk:
                        continue
                    sub.cursor = len(full_prefix)
                    sub.queue.put_nowait({"type": "delta", "chunk": chunk})
                else:
                    sub.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: kick it. Its client reconnects and the
                # fresh snapshot re-aligns it (correctness preserved).
                self.unsubscribe(task_id, sub)

    def _remember_done(self, task_id: str, event: dict[str, Any]) -> None:
        self._done_events[task_id] = event
        self._done_events.move_to_end(task_id)
        while len(self._done_events) > _MAX_DONE_CACHE:
            self._done_events.popitem(last=False)


# Process-wide default hub. The server runs single-worker (a hard
# constraint for live deltas), so one module-level instance is the shared
# bus between the report/progress write path and the SSE read path.
default_hub = StreamHub()


__all__ = ["StreamHub", "Subscriber", "default_hub"]
