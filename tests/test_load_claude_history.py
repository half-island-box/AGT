"""Tests for the Claude Code history reader (``agt/agent_history.py``).

Phase 4 chat-multisession: ``chat --resume`` prints past turns by reading
Claude Code's ``~/.claude/projects/<hash>/<sid>.jsonl`` transcripts. These
tests cover the parser (pure helpers) and the directory scan (monkeypatched
home), plus the provider dispatcher.
"""

from __future__ import annotations

import json
from pathlib import Path

from agt.agent_history import (
    Message,
    _extract_message,
    _flatten_content,
    _parse_jsonl,
    load_claude_history,
    load_history,
)


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(obj) for obj in lines) + "\n",
        encoding="utf-8",
    )


def test_flatten_content_string() -> None:
    assert _flatten_content("hello") == "hello"


def test_flatten_content_text_blocks() -> None:
    blocks = [
        {"type": "text", "text": "first"},
        {"type": "text", "text": "second"},
    ]
    assert _flatten_content(blocks) == "first\nsecond"


def test_flatten_content_ignores_non_text_blocks() -> None:
    blocks = [
        {"type": "text", "text": "keep"},
        {"type": "tool_use", "id": "x", "name": "bash"},
    ]
    assert _flatten_content(blocks) == "keep"


def test_flatten_content_none_and_empty() -> None:
    assert _flatten_content(None) is None
    assert _flatten_content(123) is None
    assert _flatten_content([]) is None


def test_extract_message_user() -> None:
    obj = {
        "type": "user",
        "message": {"role": "user", "content": "hi"},
        "timestamp": "2026-08-19T00:00:00Z",
    }
    msg = _extract_message(obj)
    assert msg is not None
    assert msg.role == "user"
    assert msg.content == "hi"


def test_extract_message_assistant_blocks() -> None:
    obj = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "answer"}],
        },
        "timestamp": 1724000000.0,
    }
    msg = _extract_message(obj)
    assert msg is not None
    assert msg.role == "assistant"
    assert msg.content == "answer"
    assert msg.timestamp == 1724000000.0


def test_extract_message_skips_non_message_types() -> None:
    assert _extract_message({"type": "system", "message": {"content": "x"}}) is None
    assert _extract_message({"type": "tool_result", "content": "x"}) is None
    # Empty content → None (nothing displayable).
    assert _extract_message({"type": "user", "message": {"content": None}}) is None


def test_parse_jsonl_flattens_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_transcript(
        p,
        [
            {"type": "user", "message": {"role": "user", "content": "q"}},
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "a"}],
                },
            },
        ],
    )
    msgs = _parse_jsonl(p)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["q", "a"]


def test_parse_jsonl_skips_bad_lines(tmp_path: Path) -> None:
    p = tmp_path / "t.jsonl"
    _write_transcript(
        p,
        [
            {"type": "user", "message": {"role": "user", "content": "ok"}},
        ],
    )
    # Append a corrupt line directly.
    with p.open("a", encoding="utf-8") as f:
        f.write("{this is not json}\n")
        f.write("\n")  # blank line is skipped too
    msgs = _parse_jsonl(p)
    assert len(msgs) == 1


def test_load_claude_history_missing_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert load_claude_history("sess-123") == []


def test_load_claude_history_finds_by_stem(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "some-hash"
    _write_transcript(
        proj / "sess-123.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "hello"}}],
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    msgs = load_claude_history("sess-123")
    assert len(msgs) == 1
    assert msgs[0].content == "hello"


def test_load_claude_history_missing_session(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "some-hash"
    _write_transcript(
        proj / "other.jsonl",
        [{"type": "user", "message": {"role": "user", "content": "x"}}],
    )
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert load_claude_history("sess-999") == []


def test_load_history_dispatcher() -> None:
    assert isinstance(load_history("claude", "x"), list)
    assert isinstance(load_history("codex", "x"), list)
    try:
        load_history("cursor", "x")
    except NotImplementedError as exc:
        assert "cursor" in str(exc)
    else:
        raise AssertionError("expected NotImplementedError for non-claude provider")


def test_message_dataclass_defaults() -> None:
    m = Message(role="user", content="x")
    assert m.timestamp is None
