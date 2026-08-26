"""Tests for StreamingSink implementations.

Phase 5: the streaming layer must consume Claude Code
``--output-format stream-json`` events and turn them into a ``SinkResult``
the executor wraps into ``TaskResult``. Each test below pins one observed
behaviour so a future Claude Code format change forces a code update
instead of a silent regression.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agt.runtime.streaming import (
    ClaudeStreamSink,
    NoopSink,
    SinkResult,
    StreamingSink,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> list[dict]:
    path = FIXTURES / name
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_claude_sink_consumes_sample_and_finalizes():
    """Happy-path sample → final ``SinkResult`` carries session_id and text."""
    sink = ClaudeStreamSink()
    events = _load_fixture("claude_stream_json_sample.jsonl")
    for evt in events:
        sink.consume(evt)
    result = sink.finalize()

    assert isinstance(result, SinkResult)
    assert result.is_error is False
    # Sample fixture was produced with a real session, so we should have
    # captured the session_id from the terminal ``result`` event.
    assert result.session_id is not None
    assert isinstance(result.session_id, str)
    # The assistant text block in the fixture is "Hello there friend" —
    # pinned to the fixture so a future Claude Code format change is
    # caught immediately.
    assert "Hello" in result.text


def test_claude_sink_swallows_unknown_event_types():
    """Unknown event ``type`` values are silently skipped — no exception."""
    sink = ClaudeStreamSink()
    sink.consume({"type": "system", "subtype": "init", "session_id": "init-sid"})
    sink.consume({"type": "totally-unknown-shape", "anything": 1})
    sink.consume({"type": "user", "message": {"role": "user", "content": "hi"}})
    result = sink.finalize()

    # session_id is captured from the terminal ``result`` event only —
    # ``system init`` is intentionally ignored (it announces the
    # session but the terminal event is the authoritative source).
    assert result.session_id is None
    assert result.text == ""


def test_claude_sink_session_id_from_result_event_only():
    """``system init`` MUST NOT pre-empt the terminal ``result`` session_id."""
    sink = ClaudeStreamSink()
    sink.consume({"type": "system", "subtype": "init", "session_id": "init-sid"})
    sink.consume(
        {
            "type": "result",
            "is_error": False,
            "session_id": "result-sid",
            "subtype": "success",
        }
    )
    result = sink.finalize()
    assert result.session_id == "result-sid"


def test_claude_sink_text_delta_uses_flush():
    """``on_text_delta`` MUST print with ``flush=True`` for live streaming UX.

    Pinning this guards against an easy regression: forgetting ``flush``
    makes the terminal appear to wait for newlines before showing any
    output, which kills the streaming effect entirely.
    """
    sink = ClaudeStreamSink()
    with patch("builtins.print") as mock_print:
        sink.on_text_delta("hello ")
        sink.on_text_delta("world")
    assert mock_print.call_count == 2
    for call in mock_print.call_args_list:
        # print(text, end="", flush=True)
        args, kwargs = call
        assert kwargs.get("flush", False) is True
        assert kwargs.get("end", None) == ""


def test_claude_sink_consume_exception_does_not_break_sink():
    """A buggy ``on_text_delta`` (raising) must not break subsequent consumption.

    Sink resilience is enforced by ``TaskExecutor._drain_json`` which
    wraps ``sink.consume`` in ``try/except``. We pin that contract here
    by running the same loop the executor uses.
    """
    import logging
    from unittest.mock import patch

    from agt.task.executor import TaskExecutor  # local import to avoid cycle

    class BrokenSink(ClaudeStreamSink):
        def on_text_delta(self, text):  # noqa: D401
            raise RuntimeError("boom")

    sink = BrokenSink()
    # The executor's drain loop swallows per-event exceptions. Simulate
    # it: a single bad frame must not stop later frames from being
    # consumed.
    events = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
        {"type": "result", "is_error": False, "session_id": "abc"},
    ]
    with patch.object(logging.getLogger("agt.task.executor"), "warning"):
        for evt in events:
            try:
                sink.consume(evt)
            except Exception:
                pass  # this is what _drain_json does
    result = sink.finalize()
    assert result.session_id == "abc"


def test_claude_sink_collects_terminal_event_for_detector():
    """The sink exposes the terminal event fields the detector needs."""
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "result",
            "is_error": True,
            "session_id": "x",
            "subtype": "error_during_execution",
            "errors": ["--resume requires a valid session ID"],
        }
    )
    assert sink.subtype == "error_during_execution"
    assert sink.errors == ["--resume requires a valid session ID"]


def test_claude_sink_resume_error_fixture():
    """Resume rejection fixture (1-line ``result`` event) is captured cleanly."""
    sink = ClaudeStreamSink()
    events = _load_fixture("claude_stream_json_resume_error.jsonl")
    for evt in events:
        sink.consume(evt)
    result = sink.finalize()

    assert result.is_error is True
    assert sink.subtype == "error_during_execution"
    assert any("resume" in e.lower() for e in sink.errors)


def test_claude_sink_skips_thinking_blocks():
    """``content`` blocks with ``type != "text"`` (e.g. thinking) MUST be ignored."""
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "reasoning..."},
                    {"type": "text", "text": "visible answer"},
                    {"type": "tool_use", "id": "t1", "name": "Bash"},
                ]
            },
        }
    )
    result = sink.finalize()
    assert result.text == "visible answer"
    # No thinking/tool_use content leaked into the user-visible text.
    assert "reasoning" not in result.text
    assert "tool_use" not in result.text


def test_noop_sink_is_abstract_safe():
    """``NoopSink`` returns a benign empty result and ignores consume calls."""
    sink = NoopSink()
    sink.consume({"type": "anything"})  # must not raise
    result = sink.finalize()
    assert result == SinkResult(session_id=None, is_error=False, resume_rejected=False, text="")


def test_streaming_sink_is_abstract():
    """``StreamingSink`` cannot be instantiated directly — it's an ABC."""
    with pytest.raises(TypeError):
        StreamingSink()  # type: ignore[abstract]


# ── Phase 5 delta streaming (--include-partial-messages) ─────────


def test_claude_sink_consumes_text_delta_stream_event():
    """``stream_event`` wrapping ``text_delta`` is unwrapped and forwarded."""
    sink = ClaudeStreamSink()
    with patch("builtins.print") as mock_print:
        sink.consume(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "He"},
                },
            }
        )
        sink.consume(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": "llo"},
                },
            }
        )
    result = sink.finalize()
    assert result.text == "Hello"
    # Both deltas printed immediately with flush=True.
    assert mock_print.call_count == 2


def test_claude_sink_skips_thinking_and_signature_deltas():
    """``thinking_delta`` and ``signature_delta`` are NOT forwarded."""
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "thinking_delta", "thinking": "secret reasoning"},
            },
        }
    )
    sink.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "signature_delta", "signature": "0xabc"},
            },
        }
    )
    sink.consume(
        {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "text_delta", "text": "visible"},
            },
        }
    )
    result = sink.finalize()
    assert result.text == "visible"
    assert "secret reasoning" not in result.text
    assert "0xabc" not in result.text


def test_claude_sink_dedups_full_assistant_after_deltas():
    """Once deltas have printed, the trailing full ``assistant`` event is suppressed.

    This is the double-print guard: ``--include-partial-messages`` emits
    ``text_delta`` chunks *and* a final ``assistant`` event containing the
    complete message. Without the guard, the full text would print twice.
    """
    sink = ClaudeStreamSink()
    # First the delta events…
    for chunk in ("He", "llo", " there"):
        sink.consume(
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "index": 1,
                    "delta": {"type": "text_delta", "text": chunk},
                },
            }
        )
    # …then the CLI's trailing full-message event.
    sink.consume(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Hello there"}]},
        }
    )
    result = sink.finalize()
    # The full assistant event was skipped — text is only the deltas.
    assert result.text == "Hello there"


def test_claude_sink_delta_fixture_end_to_end():
    """Full ``--include-partial-messages`` fixture → correct text + session_id."""
    sink = ClaudeStreamSink()
    for evt in _load_fixture("claude_stream_json_delta_sample.jsonl"):
        sink.consume(evt)
    result = sink.finalize()

    # session_id from terminal result event.
    assert result.session_id is not None
    assert result.is_error is False
    # text was built from text_delta chunks, NOT from the full assistant
    # event (which would double it). The delta fixture's text mentions the
    # moon.
    assert "moon" in result.text.lower() or "Moon" in result.text
    # No JSON markers leaked into the plain-text stdout.
    assert '{"type"' not in result.text


def test_claude_sink_legacy_no_delta_path_still_works():
    """Without any stream_event, the legacy ``assistant`` path still prints.

    Backward compatibility: providers on the legacy path (no
    ``--include-partial-messages``) feed only ``assistant`` + ``result``
    events, and the full-message text must still be captured.
    """
    sink = ClaudeStreamSink()
    sink.consume(
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "legacy full message"}]},
        }
    )
    result = sink.finalize()
    assert result.text == "legacy full message"