"""Tests for the Codex CLI history reader (``agt/agent_history.py``).

Phase 4 chat-multisession, codex provider: ``chat --resume`` prints past
turns by reading Codex's ``~/.codex/sessions/<Y>/<M>/<D>/rollout-*.jsonl``
transcripts. These tests cover the parser (pure helpers) and the directory
scan (monkeypatched home), plus the provider dispatcher.
"""

from __future__ import annotations

import json
from pathlib import Path

from agt.agent_history import (
    _extract_codex_message,
    _flatten_codex_content,
    _parse_codex_jsonl,
    _parse_timestamp,
    load_codex_history,
    load_history,
)


SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def _write_rollout(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(obj) for obj in lines) + "\n",
        encoding="utf-8",
    )


def _message_line(role: str, text: str, ts: str | None = None) -> dict:
    block_type = "input_text" if role == "user" else "output_text"
    line = {
        "timestamp": ts or "2026-04-20T16:44:37.772Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": role,
            "content": [{"type": block_type, "text": text}],
        },
    }
    return line


def test_flatten_codex_content_blocks() -> None:
    blocks = [
        {"type": "input_text", "text": "hello"},
        {"type": "input_text", "text": "world"},
    ]
    assert _flatten_codex_content(blocks) == "hello\nworld"


def test_flatten_codex_content_string_and_none() -> None:
    assert _flatten_codex_content("plain") == "plain"
    assert _flatten_codex_content(None) is None
    assert _flatten_codex_content([]) is None


def test_flatten_codex_content_ignores_tool_blocks() -> None:
    blocks = [
        {"type": "output_text", "text": "answer"},
        {"type": "function_call", "name": "exec_command", "arguments": "{}"},
    ]
    assert _flatten_codex_content(blocks) == "answer"


def test_extract_codex_message_user_and_assistant() -> None:
    user = _extract_codex_message(_message_line("user", "do the thing"))
    assert user is not None
    assert user.role == "user"
    assert user.content == "do the thing"
    assert user.timestamp is not None

    assistant = _extract_codex_message(_message_line("assistant", "done"))
    assert assistant is not None
    assert assistant.role == "assistant"


def test_extract_codex_message_skips_non_messages() -> None:
    # top-level type != response_item
    assert _extract_codex_message({"type": "event_msg", "payload": {}}) is None
    # response_item but payload not a message (e.g. reasoning)
    assert _extract_codex_message(
        {"type": "response_item", "payload": {"type": "reasoning"}}
    ) is None
    # message but role is developer/system
    assert _extract_codex_message(_message_line("developer", "system prompt")) is None


def test_parse_codex_jsonl_keeps_messages(tmp_path: Path) -> None:
    p = tmp_path / "rollout-2026-04-20T16-44-37-123e4567-e89b-12d3-a456-426614174000.jsonl"
    _write_rollout(
        p,
        [
            {"type": "session_meta", "payload": {"id": SESSION_ID}},
            _message_line("user", "q"),
            _message_line("assistant", "a"),
            {"type": "event_msg", "payload": {"type": "task_complete"}},
        ],
    )
    msgs = _parse_codex_jsonl(p)
    assert [m.role for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["q", "a"]


def test_parse_timestamp_iso_and_numeric() -> None:
    assert _parse_timestamp("2026-04-20T16:44:37.772Z") is not None
    assert _parse_timestamp(1724000000.0) == 1724000000.0
    assert _parse_timestamp("not-a-time") is None
    assert _parse_timestamp(None) is None


def test_load_codex_history_missing_dir(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    assert load_codex_history(SESSION_ID) == []


def test_load_codex_history_finds_by_stem(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    rollout = (
        home / ".codex" / "sessions" / "2026" / "04" / "20"
        / f"rollout-2026-04-20T16-44-37-{SESSION_ID}.jsonl"
    )
    _write_rollout(rollout, [_message_line("user", "hello")])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    msgs = load_codex_history(SESSION_ID)
    assert len(msgs) == 1
    assert msgs[0].content == "hello"


def test_load_codex_history_archived(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    rollout = (
        home / ".codex" / "archived_sessions" / "2026" / "04" / "20"
        / f"rollout-2026-04-20T16-44-37-{SESSION_ID}.jsonl"
    )
    _write_rollout(rollout, [_message_line("assistant", "archived reply")])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    msgs = load_codex_history(SESSION_ID)
    assert len(msgs) == 1
    assert msgs[0].content == "archived reply"


def test_load_codex_history_missing_session(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    other = (
        home / ".codex" / "sessions" / "2026" / "04" / "20"
        / "rollout-2026-04-20T16-44-37-00000000-0000-0000-0000-000000000000.jsonl"
    )
    _write_rollout(other, [_message_line("user", "x")])
    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    assert load_codex_history(SESSION_ID) == []


def test_load_history_dispatches_codex() -> None:
    assert isinstance(load_history("codex", "x"), list)
    try:
        load_history("cursor", "x")
    except NotImplementedError as exc:
        assert "cursor" in str(exc)
    else:
        raise AssertionError("expected NotImplementedError for unsupported provider")
