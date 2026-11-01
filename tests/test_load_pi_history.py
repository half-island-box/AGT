"""Tests for the Pi history reader (``agt/agent_history.py``).

Pi writes one JSONL file per session under
``~/.pi/agent/sessions/--<cwd>--/<timestamp>_<session-id>.jsonl``.
Version 2+ files are a tree; the active branch is the path ending at the
last entry. These tests build minimal files under
``AGT_PI_SESSION_DIR`` and pin branch selection, text flattening,
fallback paths, and the provider dispatcher.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agt.agent_history import (
    find_pi_session_files,
    find_provider_session_files,
    load_history,
    load_pi_history,
    pi_sessions_root,
)

SESSION_ID = "01a19656-4979-74f6-b563-a8bbd980780b"


def _write_session(
    root: Path,
    entries: list[dict],
    *,
    session_id: str = SESSION_ID,
    version: int = 3,
    filename: str | None = None,
) -> Path:
    directory = root / "--private-tmp-project--"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename or f"2026-11-01T00-00-00-000Z_{session_id}.jsonl")
    header = {
        "type": "session",
        "version": version,
        "id": session_id,
        "timestamp": "2026-11-01T00:00:00.000Z",
        "cwd": "/private/tmp/project",
    }
    lines = [json.dumps(header)] + [json.dumps(entry) for entry in entries]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _message(
    entry_id: str,
    parent_id: str | None,
    role: str,
    content: object,
    *,
    timestamp: str = "2026-11-01T00:00:01.000Z",
) -> dict:
    return {
        "type": "message",
        "id": entry_id,
        "parentId": parent_id,
        "timestamp": timestamp,
        "message": {
            "role": role,
            "content": content,
            "timestamp": 1_793_520_001_000,
        },
    }


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


@pytest.fixture()
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setenv("AGT_PI_SESSION_DIR", str(root))
    return root


def test_v1_file_is_linear(sessions_root: Path) -> None:
    _write_session(
        sessions_root,
        [
            _message("u1", None, "user", [_text("hello")]),
            _message("a1", "u1", "assistant", [_text("hi there")]),
        ],
        version=1,
    )
    messages = load_pi_history(SESSION_ID)
    assert [(m.role, m.content) for m in messages] == [
        ("user", "hello"),
        ("assistant", "hi there"),
    ]


def test_v3_returns_only_the_active_branch(sessions_root: Path) -> None:
    entries = [
        {
            "type": "model_change",
            "id": "m1",
            "parentId": None,
            "timestamp": "2026-11-01T00:00:00.000Z",
        },
        _message("u1", "m1", "user", [_text("question-a")]),
        _message("a1", "u1", "assistant", [_text("answer-a")]),
        _message("u2", "a1", "user", [_text("abandoned")]),
        _message("a2", "u2", "assistant", [_text("abandoned-answer")]),
        _message("b1", "a1", "user", [_text("question-b")]),
        _message("b2", "b1", "assistant", [_text("answer-b")]),
    ]
    _write_session(sessions_root, entries)

    messages = load_pi_history(SESSION_ID)
    assert [m.content for m in messages] == [
        "question-a",
        "answer-a",
        "question-b",
        "answer-b",
    ]


def test_assistant_thinking_and_tool_blocks_are_dropped(
    sessions_root: Path,
) -> None:
    _write_session(
        sessions_root,
        [
            _message(
                "a1",
                None,
                "assistant",
                [
                    {"type": "thinking", "thinking": "internal"},
                    _text("visible"),
                    {
                        "type": "toolCall",
                        "id": "call_1",
                        "name": "bash",
                        "arguments": {"command": "ls"},
                    },
                ],
            )
        ],
    )
    messages = load_pi_history(SESSION_ID)
    assert [(m.role, m.content) for m in messages] == [
        ("assistant", "visible")
    ]


def test_string_user_content_and_timestamp(sessions_root: Path) -> None:
    _write_session(
        sessions_root,
        [_message("u1", None, "user", "plain string")],
    )
    messages = load_pi_history(SESSION_ID)
    assert len(messages) == 1
    assert messages[0].content == "plain string"
    assert messages[0].timestamp is not None
    assert messages[0].timestamp > 1_700_000_000


def test_missing_directory_returns_empty(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGT_PI_SESSION_DIR", str(tmp_path / "absent"))
    assert load_pi_history(SESSION_ID) == []
    assert find_pi_session_files(SESSION_ID) == []


def test_malformed_lines_are_skipped(sessions_root: Path) -> None:
    directory = sessions_root / "--private-tmp-project--"
    directory.mkdir(parents=True)
    path = directory / f"2026-11-01T00-00-00-000Z_{SESSION_ID}.jsonl"
    header = {
        "type": "session",
        "version": 3,
        "id": SESSION_ID,
        "timestamp": "2026-11-01T00:00:00.000Z",
    }
    good = _message("u1", None, "user", [_text("kept")])
    path.write_text(
        json.dumps(header) + "\nnot json\n" + json.dumps(good) + "\n",
        encoding="utf-8",
    )
    assert [m.content for m in load_pi_history(SESSION_ID)] == ["kept"]


def test_find_matches_header_not_filename_suffix(sessions_root: Path) -> None:
    matching = _write_session(
        sessions_root,
        [_message("u1", None, "user", [_text("hi")])],
        filename="arbitrary-name.jsonl",
    )
    _write_session(
        sessions_root,
        [_message("u1", None, "user", [_text("wrong")])],
        session_id="different",
        filename=f"2026-11-01T00-00-00-000Z_{SESSION_ID}.jsonl",
    )
    assert find_pi_session_files(SESSION_ID) == [matching]
    assert find_provider_session_files("pi", SESSION_ID) == [matching]


def test_load_history_dispatches_to_pi(sessions_root: Path) -> None:
    _write_session(
        sessions_root,
        [_message("u1", None, "user", [_text("hello")])],
    )
    assert [m.content for m in load_history("pi", SESSION_ID)] == ["hello"]


def test_find_rejects_unsafe_session_id(sessions_root: Path) -> None:
    with pytest.raises(ValueError):
        find_pi_session_files("../escape")


def test_session_root_env_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGT_PI_SESSION_DIR", str(tmp_path / "agt"))
    monkeypatch.setenv(
        "PI_CODING_AGENT_SESSION_DIR", str(tmp_path / "pi-session")
    )
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "pi-agent"))
    assert pi_sessions_root() == tmp_path / "agt"

    monkeypatch.delenv("AGT_PI_SESSION_DIR")
    assert pi_sessions_root() == tmp_path / "pi-session"

    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    assert pi_sessions_root() == tmp_path / "pi-agent" / "sessions"

    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    assert pi_sessions_root() == Path.home() / ".pi" / "agent" / "sessions"
