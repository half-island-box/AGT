"""Tests for the Claude structured-event sink (thinking / tool / usage).

The fixture is a trimmed copy of a real Claude Code 2.1.285
``--output-format stream-json --include-partial-messages`` trace, with the
blocking shapes kept: one streamed thinking block, a text block, a tool
call spread over ``input_json_delta`` chunks, a successful and a failed
``tool_result``, and the terminal ``result`` aggregate.
"""

from __future__ import annotations

import json
from pathlib import Path

from agt.runtime.streaming import (
    MAX_EVENTS_PER_STEP,
    ClaudeStreamSink,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _feed(sink: ClaudeStreamSink, lines: list[dict]) -> None:
    for line in lines:
        sink.consume(line)


def _fixture_lines() -> list[dict]:
    path = FIXTURES / "claude_stream_json_tools_sample.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _events(raw_lines: list[dict]) -> tuple[ClaudeStreamSink, list[dict]]:
    sink = ClaudeStreamSink()
    _feed(sink, raw_lines)
    result = sink.finalize()
    return sink, list(result.events)


def test_fixture_emits_thinking_tool_result_and_usage() -> None:
    sink, events = _events(_fixture_lines())
    kinds = [event["kind"] for event in events]

    assert kinds.count("thinking_start") == 1
    assert kinds.count("thinking_delta") == 2
    assert kinds.count("thinking_end") == 1
    assert kinds.count("tool_use_start") == 1
    assert kinds.count("tool_use_end") == 1
    assert kinds.count("tool_result") == 2
    # Three API calls (one per message) + one turn aggregate.
    assert kinds.count("usage") == 4

    thinking_text = "".join(
        event.get("text", "") for event in events if event["kind"] == "thinking_delta"
    )
    assert thinking_text == "I should list the files."

    tool_end = next(event for event in events if event["kind"] == "tool_use_end")
    assert tool_end["name"] == "Bash"
    assert tool_end["tool_use_id"] == "toolu_1"
    assert tool_end["input"] == {"command": "ls -la"}

    results = [event for event in events if event["kind"] == "tool_result"]
    assert results[0]["content"] == "TOOLTEST"
    assert results[0]["is_error"] is False
    assert results[1]["is_error"] is True

    # Text still arrives on the plain-text channel exactly once.
    assert sink.finalize().text == "Let me check.Done."


def test_assistant_lines_do_not_duplicate_streamed_blocks() -> None:
    _, events = _events(_fixture_lines())
    # One thinking_start + one tool_use_start despite the assistant lines
    # repeating both blocks (the CLI emits one per content block).
    assert sum(1 for e in events if e["kind"] == "thinking_start") == 1
    assert sum(1 for e in events if e["kind"] == "tool_use_start") == 1


def test_sequence_is_monotonic_and_offsets_track_text_length() -> None:
    sink, events = _events(_fixture_lines())
    seqs = [event["seq"] for event in events]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)

    offsets = [event["text_offset"] for event in events]
    assert offsets == sorted(offsets)
    final_len = len(sink.finalize().text)
    assert offsets[-1] == final_len

    # Thinking happens before any text, tool calls after "Let me check.".
    first_tool = next(e for e in events if e["kind"] == "tool_use_start")
    assert first_tool["text_offset"] == len("Let me check.")


def test_usage_message_and_turn_scopes_are_separate() -> None:
    _, events = _events(_fixture_lines())
    usage = [event for event in events if event["kind"] == "usage"]

    message_usage = [e for e in usage if e.get("scope") == "message"]
    turn_usage = [e for e in usage if e.get("scope") == "turn"]
    assert len(message_usage) == 3
    assert len(turn_usage) == 1

    # The turn aggregate is authoritative and unchanged by the per-call
    # message usage (no addition).
    turn = turn_usage[0]
    assert turn["input_tokens"] == 440
    assert turn["output_tokens"] == 50
    assert turn["cache_read_input_tokens"] == 180
    assert turn["cache_creation_input_tokens"] == 5
    assert turn["cost_usd"] == 0.17
    assert turn["num_turns"] == 3
    assert turn["model"] == "claude-sonnet-4-5"

    # One usage event per API call: message_start input side merged with
    # message_delta output side.
    assert message_usage[0]["input_tokens"] == 100
    assert message_usage[0]["output_tokens"] == 30


def test_sink_result_carries_events_and_usage() -> None:
    sink = ClaudeStreamSink()
    _feed(sink, _fixture_lines())
    result = sink.finalize()
    assert result.events
    assert result.usage is not None
    assert result.usage["scope"] == "turn"
    assert result.session_id == "sess-1"


def test_assistant_fallback_emits_blocks_without_partial_events() -> None:
    """A CLI run without ``stream_event`` lines still yields the blocks."""
    lines = [
        {
            "type": "assistant",
            "message": {
                "id": "msg_x",
                "content": [
                    {"type": "thinking", "thinking": "hmm"},
                    {"type": "tool_use", "id": "toolu_x", "name": "Bash",
                     "input": {"command": "pwd"}},
                    {"type": "text", "text": "hi"},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "toolu_x",
                     "content": "ok", "is_error": False},
                ]
            },
        },
    ]
    sink, events = _events(lines)
    kinds = [event["kind"] for event in events]
    assert kinds == [
        "thinking_start",
        "thinking_delta",
        "thinking_end",
        "tool_use_start",
        "tool_use_end",
        "tool_result",
    ]
    assert events[5]["content"] == "ok"
    assert sink.finalize().text == "hi"


def test_large_tool_result_is_truncated() -> None:
    payload = "x" * (100 * 1024)
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t", "content": payload}
                ]
            },
        }
    )
    event = sink.finalize().events[0]
    assert event["kind"] == "tool_result"
    assert event.get("truncated") is True
    assert len(json.dumps(event, ensure_ascii=False).encode()) <= 32 * 1024
    assert "…[truncated]…" in event["content"]


def test_event_count_is_capped_and_keeps_structural_nodes() -> None:
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "thinking"},
            },
        }
    )
    for _ in range(MAX_EVENTS_PER_STEP + 100):
        sink.consume(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "x"},
                },
            }
        )
    events = sink.finalize().events
    assert len(events) == MAX_EVENTS_PER_STEP
    assert events[0]["kind"] == "thinking_start"
    assert events[-1]["kind"] == "thinking_delta"


def test_unknown_events_are_ignored() -> None:
    sink = ClaudeStreamSink()
    _feed(
        sink,
        [
            {"type": "system", "subtype": "status", "status": "..."},
            {"type": "stream_event", "event": {"type": "message_stop"}},
            {"type": "totally_unknown"},
        ],
    )
    assert sink.finalize().events == []


def test_signature_delta_is_dropped() -> None:
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "sig"},
            },
        }
    )
    assert sink.finalize().events == []
