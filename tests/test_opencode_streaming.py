"""OpenCode ``run --format json`` sink.

Fixtures below are real event lines captured from opencode 2.2.5, so the
tests pin the shapes the CLI actually emits (session ids on every line,
whole text parts rather than token deltas, completed tool parts, and a
``tokens``/``cost`` summary on ``step_finish``).
"""

from __future__ import annotations

import contextlib
import io
import json

from agt.runtime.streaming import OpenCodeStreamSink


def _consume(sink: OpenCodeStreamSink, *raws: dict) -> str:
    """Feed ``raws`` to ``sink`` and return what was printed to stdout."""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        for raw in raws:
            sink.consume(raw)
        result = sink.finalize()
    assert result is not None
    return buffer.getvalue()


def _text(part_id: str, text: str) -> dict:
    return {
        "type": "text",
        "sessionID": "ses_test",
        "part": {"id": part_id, "type": "text", "text": text},
    }


def test_text_parts_print_live_with_paragraph_separator() -> None:
    sink = OpenCodeStreamSink()
    printed = _consume(sink, _text("p1", "Hello"), _text("p2", "World"))
    assert printed == "Hello\n\nWorld"
    assert sink.finalize().text == "Hello\n\nWorld"


def test_replayed_text_part_is_not_duplicated() -> None:
    """Reconcile may re-emit a full part; only the unseen suffix is new."""
    sink = OpenCodeStreamSink()
    printed = _consume(
        sink,
        _text("p1", "Hello"),
        _text("p1", "Hello"),
        _text("p1", "Hello world"),
    )
    assert printed == "Hello world"


def test_session_id_captured_from_any_event() -> None:
    sink = OpenCodeStreamSink()
    _consume(sink, {"type": "step_start", "sessionID": "ses_abc", "part": {}})
    assert sink.finalize().session_id == "ses_abc"


def test_tool_use_emits_start_end_and_result() -> None:
    sink = OpenCodeStreamSink()
    raw = {
        "type": "tool_use",
        "sessionID": "ses_test",
        "part": {
            "id": "part_tool",
            "type": "tool",
            "tool": "read",
            "state": {
                "status": "completed",
                "input": {"path": "note.txt"},
                "output": "1: hello from a file",
            },
        },
    }
    _consume(sink, raw)
    events = list(sink.events)
    assert [e["kind"] for e in events] == [
        "tool_use_start",
        "tool_use_end",
        "tool_result",
    ]
    assert events[0]["name"] == "read"
    assert events[1]["input"] == {"path": "note.txt"}
    assert events[2]["content"] == "1: hello from a file"
    assert events[2]["is_error"] is False


def test_failed_tool_use_reports_error() -> None:
    sink = OpenCodeStreamSink()
    raw = {
        "type": "tool_use",
        "sessionID": "ses_test",
        "part": {
            "id": "part_tool",
            "type": "tool",
            "tool": "bash",
            "state": {"status": "error", "input": {}, "error": "boom"},
        },
    }
    _consume(sink, raw)
    result_event = [e for e in sink.events if e["kind"] == "tool_result"][0]
    assert result_event["is_error"] is True
    assert result_event["content"] == "boom"


def test_reasoning_emits_one_thinking_block() -> None:
    sink = OpenCodeStreamSink()
    raw = {
        "type": "reasoning",
        "sessionID": "ses_test",
        "part": {"id": "part_reason", "type": "reasoning", "text": "thinking"},
    }
    _consume(sink, raw, raw)
    kinds = [e["kind"] for e in sink.events]
    assert kinds == ["thinking_start", "thinking_delta", "thinking_end"]
    assert sink.events[1]["text"] == "thinking"


def test_step_finish_usage_and_cost_are_aggregated() -> None:
    sink = OpenCodeStreamSink()
    _consume(
        sink,
        {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "style": "step-finish",
                "reason": "tool-calls",
                "cost": 0.25,
                "tokens": {
                    "input": 100,
                    "output": 5,
                    "reasoning": 3,
                    "cache": {"read": 40, "write": 2},
                },
            },
        },
        {
            "type": "step_finish",
            "sessionID": "ses_test",
            "part": {
                "style": "step-finish",
                "reason": "stop",
                "cost": 0.5,
                "tokens": {
                    "input": 200,
                    "output": 7,
                    "reasoning": 1,
                    "cache": {"read": 60, "write": 0},
                },
            },
        },
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
    message_events = [e for e in sink.events if e.get("scope") == "message"]
    assert len(message_events) == 2


def test_error_event_sets_failed_status() -> None:
    sink = OpenCodeStreamSink()
    raw = {
        "type": "error",
        "sessionID": "ses_test",
        "error": {
            "type": "provider.error",
            "message": "model unavailable",
        },
    }
    _consume(sink, raw)
    result = sink.finalize()
    assert result.is_error is True
    assert sink.errors == ["model unavailable"]


def test_unknown_event_types_are_ignored() -> None:
    sink = OpenCodeStreamSink()
    _consume(
        sink,
        {"type": "step_start", "sessionID": "ses_test", "part": {}},
        {"type": "future_event", "sessionID": "ses_test", "part": {}},
    )
    assert sink.finalize().is_error is False


def test_json_lines_parse_individually() -> None:
    """The executor feeds each stdout line through ``json.loads``."""
    sink = OpenCodeStreamSink()
    line = json.dumps(_text("p1", "hi"))
    _consume(sink, json.loads(line))
    assert sink.finalize().text == "hi"
