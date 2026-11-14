"""Pi ``--mode json`` sink.

Fixtures mirror real pi 1.4.0 / session-format-v3 output: a session
header, delta-only ``message_update`` records keyed by ``contentIndex``,
authoritative ``message_end`` messages, and ``tool_execution_*``
lifecycle records. JSON mode exits 0 on a model error, so the tests pin
the in-band error paths.
"""

from __future__ import annotations

import contextlib
import io

from agt.runtime.streaming import PiStreamSink

SESSION_ID = "01a19656-4979-74f6-b563-a8bbd980780b"


def _consume(sink: PiStreamSink, *raws: dict) -> str:
    """Feed ``raws`` to ``sink`` and return what was printed to stdout."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        for raw in raws:
            sink.consume(raw)
        sink.finalize()
    return buffer.getvalue()


def _session() -> dict:
    return {"type": "session", "version": 3, "id": SESSION_ID}


def _assistant_start(stop: str = "pending") -> dict:
    return {
        "type": "message_start",
        "message": {
            "role": "assistant",
            "content": [],
            "stopReason": stop,
            "usage": {},
        },
    }


def _update(event: dict) -> dict:
    return {"type": "message_update", "assistantMessageEvent": event}


def _text_delta(index: int, delta: str) -> dict:
    return _update(
        {"type": "text_delta", "contentIndex": index, "delta": delta}
    )


def _assistant_end(
    content: list[dict],
    *,
    stop: str = "stop",
    usage: dict | None = None,
    error: str | None = None,
) -> dict:
    message = {
        "role": "assistant",
        "content": content,
        "stopReason": stop,
        "usage": usage or {},
    }
    if error:
        message["errorMessage"] = error
    return {"type": "message_end", "message": message}


def _usage(
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read: int = 0,
    cache_write: int = 0,
    cost: float = 0.0,
) -> dict:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "reasoning": 0,
        "totalTokens": input_tokens + output_tokens,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
            "total": cost,
        },
    }


def test_session_id_and_text_deltas_are_captured() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _text_delta(0, "He"),
        _text_delta(0, "llo"),
        _assistant_end([{"type": "text", "text": "Hello"}]),
    )
    assert printed == "Hello"
    assert sink.finalize().session_id == SESSION_ID


def test_text_end_reconciles_without_duplicating() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _text_delta(0, "Hel"),
        _update({"type": "text_end", "contentIndex": 0, "content": "Hello"}),
        _assistant_end([{"type": "text", "text": "Hello"}]),
    )
    assert printed == "Hello"


def test_message_end_fallback_renders_text_without_deltas() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _assistant_end([{"type": "text", "text": "buffered"}]),
    )
    assert printed == "buffered"


def test_successive_assistant_messages_are_separated() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _text_delta(0, "one"),
        _assistant_end([{"type": "text", "text": "one"}]),
        _assistant_start(),
        _text_delta(0, "two"),
        _assistant_end([{"type": "text", "text": "two"}]),
    )
    assert printed == "one\n\ntwo"


def test_provider_paragraph_boundary_is_not_doubled() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _text_delta(0, "one\n\n"),
        _assistant_end([{"type": "text", "text": "one\n\n"}]),
        _assistant_start(),
        _text_delta(0, "two"),
        _assistant_end([{"type": "text", "text": "two"}]),
    )
    assert printed == "one\n\ntwo"


def test_thinking_emits_one_block_and_is_not_rendered_as_text() -> None:
    sink = PiStreamSink()
    printed = _consume(
        sink,
        _session(),
        _assistant_start(),
        _update({"type": "thinking_start", "contentIndex": 0}),
        _update(
            {
                "type": "thinking_delta",
                "contentIndex": 0,
                "delta": "hmm",
            }
        ),
        _update(
            {
                "type": "thinking_end",
                "contentIndex": 0,
                "content": "hmm",
            }
        ),
        _assistant_end(
            [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "answer"},
            ]
        ),
    )
    assert printed == "answer"
    kinds = [event["kind"] for event in sink.finalize().events]
    assert kinds == ["thinking_start", "thinking_delta", "thinking_end"]


def test_tool_call_and_execution_emit_structured_events() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        _assistant_start(),
        _update(
            {
                "type": "toolcall_start",
                "contentIndex": 0,
                "id": "call_1",
                "toolName": "bash",
            }
        ),
        _update(
            {
                "type": "toolcall_end",
                "contentIndex": 0,
                "toolCall": {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "bash",
                    "arguments": {"command": "ls"},
                },
            }
        ),
        {
            "type": "tool_execution_start",
            "toolCallId": "call_1",
            "toolName": "bash",
            "args": {"command": "ls"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call_1",
            "toolName": "bash",
            "result": {
                "content": [{"type": "text", "text": "file.txt"}],
                "details": {},
            },
            "isError": False,
        },
        _assistant_end(
            [
                {
                    "type": "toolCall",
                    "id": "call_1",
                    "name": "bash",
                    "arguments": {"command": "ls"},
                },
                {"type": "text", "text": "done"},
            ]
        ),
    )
    events = list(sink.finalize().events)
    kinds = [event["kind"] for event in events]
    assert kinds == ["tool_use_start", "tool_use_end", "tool_result"]
    assert events[1]["input"] == {"command": "ls"}
    assert events[2]["content"] == "file.txt"
    assert events[2]["is_error"] is False


def test_tool_execution_without_toolcall_still_opens_and_closes() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        {
            "type": "tool_execution_start",
            "toolCallId": "call_2",
            "toolName": "read",
            "args": {"path": "a.txt"},
        },
        {
            "type": "tool_execution_end",
            "toolCallId": "call_2",
            "toolName": "read",
            "result": {"content": [{"type": "text", "text": "boom"}]},
            "isError": True,
        },
    )
    events = list(sink.finalize().events)
    assert [event["kind"] for event in events] == [
        "tool_use_start",
        "tool_use_end",
        "tool_result",
    ]
    assert events[2]["is_error"] is True


def test_usage_and_cost_are_aggregated() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        _assistant_start(),
        _assistant_end(
            [{"type": "text", "text": "a"}],
            usage=_usage(100, 5, cache_read=40, cache_write=2, cost=0.25),
        ),
        _assistant_start(),
        _assistant_end(
            [{"type": "text", "text": "b"}],
            usage=_usage(200, 7, cache_read=60, cost=0.5),
        ),
    )
    result = sink.finalize()
    assert result.usage == {
        "input_tokens": 300,
        "output_tokens": 12,
        "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 2,
        "scope": "turn",
        "num_turns": 2,
        "cost_usd": 0.75,
    }
    message_usage = [
        event for event in result.events if event.get("scope") == "message"
    ]
    assert len(message_usage) == 2


def test_message_stop_reason_error_sets_failed_status() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        _assistant_start(),
        _assistant_end([], stop="error", error="model unavailable"),
    )
    result = sink.finalize()
    assert result.is_error is True
    assert sink.errors == ["model unavailable"]
    assert sink.subtype == "error"


def test_auto_retry_failure_sets_failed_status() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        {
            "type": "auto_retry_end",
            "success": False,
            "attempt": 1,
            "finalError": "401 invalid x-api-key",
        },
    )
    result = sink.finalize()
    assert result.is_error is True
    assert sink.errors == ["401 invalid x-api-key"]


def test_agent_settled_aborted_sets_failed_status() -> None:
    sink = PiStreamSink()
    _consume(sink, _session(), {"type": "agent_settled", "aborted": True})
    assert sink.finalize().is_error is True


def test_unknown_events_are_ignored() -> None:
    sink = PiStreamSink()
    _consume(
        sink,
        _session(),
        {"type": "future_event", "payload": {"x": 1}},
        {"type": "agent_start"},
        {"type": "turn_start"},
    )
    assert sink.finalize().is_error is False
