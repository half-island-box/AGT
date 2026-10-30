"""Storage + fan-out tests for structured agent events and turn usage.

Covers the server half of Requirement 2:

- ``step_events`` rows are written by progress batches and by the report
  tail, idempotently on ``(task_id, seq)``;
- events are recorded even when the text prefix did not grow (a tool call
  produces no visible text);
- turn usage lands on the Step columns and in the ``done`` frame;
- the SSE stream replays stored events in the snapshot and forwards fresh
  ones as ``agent_events``, respecting the per-subscriber event cursor;
- the ``/events`` history endpoints read the same wire shape back.

DB setup mirrors ``test_task_progress.py``.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import delete, func, select

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, StepEventModel, StepModel
from agt.server.routes.tasks import stream_task
from agt.server.services.report_service import (
    events_for_step,
    normalize_agent_events,
    record_step_progress_for_daemon,
    record_step_result_for_daemon,
    step_usage_out,
)
from agt.server.stream_hub import StreamHub, default_hub

SECRET = "agent-events-test-secret"


def _utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _fresh_engine(db_path: str):
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=db_path,
        secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_agent_events.json",
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(StepEventModel))
            await db.execute(delete(StepModel))
            await db.commit()

    asyncio.run(_setup())
    return engine


async def _seed_task(
    factory,
    task_id: str,
    daemon_id: str | None,
    *,
    status: str = "running",
    stdout: str | None = None,
) -> None:
    now = _utcnow()
    async with session_scope(factory) as db:
        db.add(
            StepModel(
                step_id=task_id,
                provider="claude",
                prompt="hi",
                status=status,
                daemon_id=daemon_id,
                stdout=stdout,
                created_at=now,
                updated_at=now,
            )
        )
        await db.commit()


async def _event_rows(factory, task_id: str) -> int:
    async with session_scope(factory) as db:
        return int(
            await db.scalar(
                select(func.count())
                .select_from(StepEventModel)
                .where(StepEventModel.step_id == task_id)
            )
            or 0
        )


def _ev(seq: int, kind: str, **payload) -> dict:
    return {"seq": seq, "kind": kind, "text_offset": 0, **payload}


# ── normalize_agent_events ─────────────────────────────────────────


def test_normalize_agent_events_sorts_dedupes_and_drops_malformed() -> None:
    raw = [
        _ev(3, "tool_result", tool_use_id="t", content="ok"),
        _ev(1, "thinking_start"),
        _ev(1, "thinking_start"),  # duplicate seq
        {"kind": "usage"},  # missing seq
        {"seq": 2, "kind": ""},  # empty kind
        {"seq": True, "kind": "usage"},  # bool is not a seq
        "not-a-dict",
    ]
    out = normalize_agent_events(raw)
    assert [event["seq"] for event in out] == [1, 3]
    assert out[0]["payload"] == {}
    assert out[1]["payload"] == {"tool_use_id": "t", "content": "ok"}

    # A text_offset that isn't an int falls back to 0 instead of failing.
    assert normalize_agent_events([{"seq": 0, "kind": "x", "text_offset": "?"}])[0][
        "text_offset"
    ] == 0


def test_normalize_agent_events_accepts_pydantic_models() -> None:
    from agt.server.schemas import AgentEvent

    out = normalize_agent_events(
        [AgentEvent(seq=1, kind="usage", text_offset=5, scope="turn")]
    )
    assert out == [
        {"seq": 1, "kind": "usage", "text_offset": 5, "payload": {"scope": "turn"}}
    ]


# ── progress persistence ───────────────────────────────────────────


def test_progress_records_events_without_text_growth() -> None:
    engine = _fresh_engine("/tmp/_agent_events_progress.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-progress", "d1", stdout="hi")
        sub = default_hub.subscribe("t-ev-progress", cursor=2, event_cursor=0)
        async with session_scope(factory) as db:
            result = await record_step_progress_for_daemon(
                db,
                daemon_id="d1",
                step_id="t-ev-progress",
                stdout_prefix="hi",  # no growth
                events=[
                    _ev(1, "tool_use_start", tool_use_id="tu", name="Bash"),
                    _ev(2, "tool_use_end", tool_use_id="tu", name="Bash",
                        input={"command": "ls"}),
                ],
            )
        assert result == {"recorded": True, "last_seq": 2}

        async with session_scope(factory) as db:
            step = await db.get(StepModel, "t-ev-progress")
            assert step.stdout == "hi"  # text untouched
            events = await events_for_step(db, "t-ev-progress")
        assert [e["seq"] for e in events] == [1, 2]
        assert events[0]["kind"] == "tool_use_start"
        assert events[1]["input"] == {"command": "ls"}

        published = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert published["type"] == "agent_events"
        assert [e["seq"] for e in published["events"]] == [1, 2]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_repeated_event_batch_is_idempotent() -> None:
    engine = _fresh_engine("/tmp/_agent_events_idempotent.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-dupe", "d1", stdout="hi")
        batch = [_ev(1, "thinking_start"), _ev(2, "thinking_end")]
        async with session_scope(factory) as db:
            first = await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-dupe",
                stdout_prefix="hi", events=batch,
            )
        assert first == {"recorded": True, "last_seq": 2}

        # Replay of the same batch: no row growth, no publish, no ack.
        sub = default_hub.subscribe("t-ev-dupe", cursor=2, event_cursor=0)
        async with session_scope(factory) as db:
            second = await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-dupe",
                stdout_prefix="hi", events=batch,
            )
        assert second == {"recorded": False}
        assert await _event_rows(factory, "t-ev-dupe") == 2
        assert sub.queue.empty()

        # An overlapping tail (new + already-stored seqs) records only the
        # new rows.
        async with session_scope(factory) as db:
            third = await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-dupe",
                stdout_prefix="hi", events=[_ev(2, "thinking_end"), _ev(3, "usage")],
            )
        assert third == {"recorded": True, "last_seq": 3}
        assert await _event_rows(factory, "t-ev-dupe") == 3

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_progress_without_events_keeps_legacy_response_shape() -> None:
    engine = _fresh_engine("/tmp/_agent_events_legacy.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-legacy", "d1", stdout="")
        async with session_scope(factory) as db:
            result = await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-legacy", stdout_prefix="new",
            )
        # Byte-identical to the pre-Requirement-2 response.
        assert result == {"recorded": True}
        assert await _event_rows(factory, "t-ev-legacy") == 0

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── report: usage + event tail ─────────────────────────────────────


def test_report_persists_usage_and_event_tail() -> None:
    engine = _fresh_engine("/tmp/_agent_events_report.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-report", "d1", stdout="hi")
        sub = default_hub.subscribe("t-ev-report", cursor=2, event_cursor=0)
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id="t-ev-report",
                exit_code=0,
                stdout="hello",
                stderr="",
                duration=1.0,
                status="completed",
                session_id="sess-ev",
                resume_rejected=False,
                usage={
                    "input_tokens": 440,
                    "output_tokens": 50,
                    "cache_read_input_tokens": 180,
                    "cache_creation_input_tokens": 5,
                    "cost_usd": 0.17,
                    "num_turns": 3,
                    "model": "claude-sonnet-4-5",
                },
                agent_events=[_ev(1, "tool_result", tool_use_id="tu", content="ok")],
            )

        async with session_scope(factory) as db:
            step = await db.get(StepModel, "t-ev-report")
            assert step.input_tokens == 440
            assert step.output_tokens == 50
            assert step.cache_read_tokens == 180
            assert step.cache_creation_tokens == 5
            assert step.cost_usd == 0.17
            assert step.num_turns == 3
            assert step.model == "claude-sonnet-4-5"
            assert step_usage_out(step) == {
                "input_tokens": 440,
                "output_tokens": 50,
                "cache_read_input_tokens": 180,
                "cache_creation_input_tokens": 5,
                "cost_usd": 0.17,
                "model": "claude-sonnet-4-5",
                "num_turns": 3,
            }
            assert len(await events_for_step(db, "t-ev-report")) == 1

        # Events are published before the terminal frame so a client can
        # render the transcript in order.
        first = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert first == {
            "type": "agent_events",
            "events": [{"seq": 1, "kind": "tool_result", "text_offset": 0,
                        "tool_use_id": "tu", "content": "ok"}],
        }
        done = await asyncio.wait_for(sub.queue.get(), timeout=1)
        assert done["type"] == "done"
        assert done["usage"]["input_tokens"] == 440
        assert done["model"] == "claude-sonnet-4-5"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_legacy_report_without_usage_leaves_columns_empty() -> None:
    engine = _fresh_engine("/tmp/_agent_events_legacy_report.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-legacy-report", "d1", stdout="")
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id="t-ev-legacy-report",
                exit_code=0,
                stdout="done",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
            )
        async with session_scope(factory) as db:
            step = await db.get(StepModel, "t-ev-legacy-report")
            assert step_usage_out(step) is None

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── SSE stream ─────────────────────────────────────────────────────


def _fake_request(factory):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_factory=factory))
    )


def _parse_frame(frame: str) -> dict:
    event_type = "message"
    data: dict | None = None
    for line in frame.splitlines():
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            data = json.loads(line[len("data:"):].strip())
    assert data is not None, f"no data line in frame: {frame!r}"
    data.setdefault("type", event_type)
    return data


async def _collect(resp) -> list[dict]:
    frames: list[dict] = []
    async for chunk in resp.body_iterator:
        frames.append(_parse_frame(chunk))
    return frames


def test_stream_snapshot_replays_events_then_forwards_new_ones() -> None:
    engine = _fresh_engine("/tmp/_agent_events_stream.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-stream", "d1", stdout="hi")
        async with session_scope(factory) as db:
            await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-stream",
                stdout_prefix="hi",
                events=[_ev(1, "thinking_start"), _ev(2, "thinking_end")],
            )

        resp = await stream_task("t-ev-stream", _fake_request(factory))
        reader = asyncio.create_task(_collect(resp))
        await asyncio.sleep(0.05)

        # Only seq 3 is new → the stored 1-2 must not be re-sent.
        async with session_scope(factory) as db:
            await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-stream",
                stdout_prefix="hi",
                events=[_ev(3, "tool_use_start", tool_use_id="tu", name="Bash")],
            )
        await asyncio.sleep(0.05)

        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id="t-ev-stream",
                exit_code=0,
                stdout="hi",
                stderr="",
                duration=0.2,
                status="completed",
                session_id="sess-stream",
                resume_rejected=False,
                usage={"input_tokens": 10, "output_tokens": 2,
                       "model": "claude-sonnet-4-5"},
            )

        frames = await asyncio.wait_for(reader, timeout=2)
        types = [frame["type"] for frame in frames]
        assert types == ["snapshot", "agent_events", "done"]

        snapshot = frames[0]
        assert snapshot["stdout"] == "hi"
        assert [e["seq"] for e in snapshot["events"]] == [1, 2]
        assert snapshot["event_cursor"] == 2
        assert snapshot["usage"] is None

        forwarded = frames[1]["events"]
        assert [e["seq"] for e in forwarded] == [3]
        assert forwarded[0]["kind"] == "tool_use_start"

        done = frames[2]
        assert done["usage"]["input_tokens"] == 10
        assert done["model"] == "claude-sonnet-4-5"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_stream_terminal_task_snapshot_carries_events_and_usage() -> None:
    engine = _fresh_engine("/tmp/_agent_events_terminal.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        await _seed_task(factory, "t-ev-term", "d1", stdout="hi")
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id="t-ev-term",
                exit_code=0,
                stdout="hi",
                stderr="",
                duration=0.2,
                status="completed",
                session_id=None,
                resume_rejected=False,
                usage={"input_tokens": 7, "output_tokens": 3},
                agent_events=[_ev(1, "tool_result", tool_use_id="tu", content="ok")],
            )

        # Unique task id → the hub has no cached done; the endpoint reads
        # the terminal state from the DB and short-circuits.
        resp = await stream_task("t-ev-term", _fake_request(factory))
        frames = await _collect(resp)
        assert [frame["type"] for frame in frames] == ["snapshot", "done"]
        assert frames[0]["event_cursor"] == 1
        assert frames[0]["events"][0]["content"] == "ok"
        assert frames[0]["usage"]["input_tokens"] == 7
        assert frames[1]["usage"]["output_tokens"] == 3

    asyncio.run(_run())
    asyncio.run(engine.dispose())


# ── hub fan-out rules ──────────────────────────────────────────────


def test_hub_suppresses_events_at_or_below_cursor() -> None:
    hub = StreamHub()
    sub = hub.subscribe("t", cursor=0, event_cursor=2)
    hub.publish("t", {"type": "agent_events",
                      "events": [_ev(1, "thinking_start"), _ev(2, "thinking_end")]})
    assert sub.queue.empty()
    assert sub.event_cursor == 2

    hub.publish("t", {"type": "agent_events",
                      "events": [_ev(2, "thinking_end"), _ev(3, "usage")]})
    event = asyncio.run(sub.queue.get())
    assert [e["seq"] for e in event["events"]] == [3]
    assert sub.event_cursor == 3


# ── /events history endpoint ───────────────────────────────────────


def test_task_events_endpoint_returns_wire_shape() -> None:
    engine = _fresh_engine("/tmp/_agent_events_route.db")
    factory = make_session_factory(engine)

    async def _run() -> None:
        from agt.server.routes.tasks import task_events

        await _seed_task(factory, "t-ev-route", "d1", stdout="hi")
        async with session_scope(factory) as db:
            await record_step_progress_for_daemon(
                db, daemon_id="d1", step_id="t-ev-route",
                stdout_prefix="hi",
                events=[
                    _ev(1, "thinking_start"),
                    _ev(2, "tool_use_end", tool_use_id="tu", name="Bash",
                        input={"command": "ls"}),
                ],
            )

        async with session_scope(factory) as db:
            payload = await task_events("t-ev-route", db)
        assert payload["task_id"] == "t-ev-route"
        assert [e["seq"] for e in payload["events"]] == [1, 2]
        assert payload["events"][1]["input"] == {"command": "ls"}
        assert payload["usage"] is None

    asyncio.run(_run())
    asyncio.run(engine.dispose())
