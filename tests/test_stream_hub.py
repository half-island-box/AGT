"""Unit tests for ``agt.server.stream_hub.StreamHub``.

Phase 5 remote streaming: the hub is the in-process bus between the
report/progress write path and the SSE read path. These tests pin its
contract — per-subscriber cursors, verbatim ``done`` delivery + replay,
unsubscribe, and kick-slow-consumers-on-full.

Each test drives a fresh :class:`StreamHub` so the module-level
``default_hub`` singleton's state never leaks between tests.
"""

from __future__ import annotations

import asyncio

from agt.server.stream_hub import StreamHub


def _run(coro):
    return asyncio.run(coro)


def test_subscribe_publish_progress_emits_delta() -> None:
    hub = StreamHub()

    async def go():
        sub = hub.subscribe("t1", cursor=0)
        hub.publish("t1", {"type": "progress", "stdout_prefix": "hello"})
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert event == {"type": "delta", "chunk": "hello"}
        # Cursor advanced to the full prefix length.
        assert sub.cursor == len("hello")

    _run(go())


def test_progress_publishes_only_growth_beyond_cursor() -> None:
    """A second, longer prefix emits only the new tail — not the whole thing."""
    hub = StreamHub()

    async def go():
        sub = hub.subscribe("t1", cursor=0)
        hub.publish("t1", {"type": "progress", "stdout_prefix": "hello"})
        first = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert first == {"type": "delta", "chunk": "hello"}

        hub.publish("t1", {"type": "progress", "stdout_prefix": "hello world"})
        second = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert second == {"type": "delta", "chunk": " world"}
        assert sub.cursor == len("hello world")

    _run(go())


def test_shrunk_prefix_emits_no_delta() -> None:
    """A prefix shorter than the cursor produces no delta (server already
    rejects shrink; the hub is the second line of defence)."""
    hub = StreamHub()

    async def go():
        sub = hub.subscribe("t1", cursor=10)
        hub.publish("t1", {"type": "progress", "stdout_prefix": "short"})
        assert sub.queue.empty()

    _run(go())


def test_done_event_delivered_verbatim_and_replayed() -> None:
    """A subscriber arriving after the report still receives ``done``."""
    hub = StreamHub()

    async def go():
        done = {"type": "done", "status": "completed", "stdout": "x"}
        # No subscribers yet — publish just remembers it.
        hub.publish("t1", done)
        sub = hub.subscribe("t1", cursor=0)
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert event == done

    _run(go())


def test_done_delivered_to_existing_subscriber() -> None:
    hub = StreamHub()

    async def go():
        sub = hub.subscribe("t1", cursor=0)
        done = {"type": "done", "status": "failed", "stdout": ""}
        hub.publish("t1", done)
        event = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert event == done

    _run(go())


def test_unsubscribe_stops_delivery() -> None:
    hub = StreamHub()

    async def go():
        sub = hub.subscribe("t1", cursor=0)
        hub.unsubscribe("t1", sub)
        hub.publish("t1", {"type": "progress", "stdout_prefix": "x"})
        assert sub.queue.empty()
        # Subscriber set is cleaned up.
        assert "t1" not in hub._subscribers

    _run(go())


def test_slow_consumer_is_kicked_on_queue_full() -> None:
    """Bounded queue + ``put_nowait``: a consumer that never drains is
    dropped rather than stalling the write path."""
    hub = StreamHub(maxsize=1)

    async def go():
        sub = hub.subscribe("t1", cursor=0)
        # First publish fills the single-slot queue.
        hub.publish("t1", {"type": "progress", "stdout_prefix": "a"})
        assert not sub.queue.empty()
        # Second publish can't enqueue → subscriber is kicked.
        hub.publish("t1", {"type": "progress", "stdout_prefix": "ab"})
        remaining = hub._subscribers.get("t1", set())
        assert sub not in remaining

    _run(go())
