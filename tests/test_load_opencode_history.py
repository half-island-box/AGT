"""Tests for the OpenCode history reader (``agt/agent_history.py``).

OpenCode 2.x keeps sessions in SQLite (``opencode.db``) instead of one
JSONL file per session. These tests build a minimal ``session_message``
table, point ``AGT_OPENCODE_DB`` at it, and cover the row mapping, the
best-effort error paths, and the provider dispatcher.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from agt.agent_history import (
    _flatten_opencode_content,
    _opencode_db_path,
    _opencode_timestamp,
    load_history,
    load_opencode_history,
)

SESSION_ID = "ses_e69fde1e8ffe76XWze9fJXAuJ7"


def _create_db(
    path: Path,
    rows: list[tuple[int, str, object]],
    *,
    table: bool = True,
) -> Path:
    """Write a minimal ``session_message`` table.

    ``rows`` is ``(seq, type, data)``; ``data`` may be a dict or a raw
    string (used for malformed-JSON cases).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        if table:
            connection.execute(
                """
                CREATE TABLE session_message (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    time_created INTEGER NOT NULL,
                    time_updated INTEGER NOT NULL,
                    data TEXT NOT NULL
                )
                """
            )
        for message_id, (seq, message_type, data) in enumerate(rows):
            raw = data if isinstance(data, str) else json.dumps(data)
            connection.execute(
                "INSERT INTO session_message"
                " (id, session_id, type, seq, time_created, time_updated, data)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"msg_{message_id}", SESSION_ID, message_type, seq, 0, 0, raw),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def _user(text: str, created_ms: int = 1_793_518_083_819) -> dict:
    return {"time": {"created": created_ms}, "text": text, "files": []}


def _assistant(
    *blocks: dict,
    created_ms: int = 1_793_518_083_826,
) -> dict:
    return {"time": {"created": created_ms}, "content": list(blocks)}


def _text(text: str) -> dict:
    return {"type": "text", "text": text}


def test_loads_user_and_assistant_in_seq_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [
            (5, "assistant", _assistant(_text("answer"))),
            (3, "user", _user("question")),
        ],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    history = load_opencode_history(SESSION_ID)

    assert [m.role for m in history] == ["user", "assistant"]
    assert [m.content for m in history] == ["question", "answer"]
    assert history[0].timestamp == pytest.approx(1_793_518_083.819)
    assert history[1].timestamp == pytest.approx(1_793_518_083.826)


def test_assistant_keeps_only_text_blocks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [
            (
                1,
                "assistant",
                _assistant(
                    {"type": "reasoning", "text": "internal"},
                    _text("first"),
                    {
                        "type": "tool",
                        "name": "read",
                        "state": {"status": "completed", "output": "file"},
                    },
                    _text("second"),
                ),
            ),
        ],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    history = load_opencode_history(SESSION_ID)

    assert [m.content for m in history] == ["first\nsecond"]


def test_assistant_without_text_is_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [
            (1, "assistant", _assistant({"type": "reasoning", "text": "only"})),
            (2, "user", _user("next")),
        ],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    history = load_opencode_history(SESSION_ID)

    assert [m.content for m in history] == ["next"]


def test_non_message_rows_are_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [
            (1, "user", _user("hi")),
            (2, "idle", {"time": {"created": 1}, "outcome": "succeeded"}),
            (3, "model-switched", {"time": {"created": 2}}),
        ],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert [m.content for m in load_opencode_history(SESSION_ID)] == ["hi"]


def test_malformed_rows_are_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [
            (1, "user", "{not json"),
            (2, "user", [1, 2, 3]),
            (3, "user", _user("survives")),
        ],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert [m.content for m in load_opencode_history(SESSION_ID)] == ["survives"]


def test_missing_db_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGT_OPENCODE_DB", str(tmp_path / "absent.db"))

    assert load_opencode_history(SESSION_ID) == []


def test_missing_table_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [], table=False)
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert load_opencode_history(SESSION_ID) == []


def test_corrupt_db_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "opencode.db"
    db.write_bytes(b"not a sqlite database")
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert load_opencode_history(SESSION_ID) == []


def test_unknown_session_returns_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [(1, "user", _user("hi"))],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert load_opencode_history("ses_unknown") == []


def test_invalid_session_id_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGT_OPENCODE_DB", str(tmp_path / "opencode.db"))

    with pytest.raises(ValueError):
        load_opencode_history("../escape")


def test_load_history_dispatches_opencode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(
        tmp_path / "opencode.db",
        [(1, "user", _user("dispatched"))],
    )
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))

    assert [m.content for m in load_history("opencode", SESSION_ID)] == [
        "dispatched"
    ]


def test_db_path_override_and_xdg(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AGT_OPENCODE_DB", str(tmp_path / "custom.db"))
    assert _opencode_db_path() == tmp_path / "custom.db"

    monkeypatch.delenv("AGT_OPENCODE_DB")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert _opencode_db_path() == tmp_path / "opencode" / "opencode.db"

    monkeypatch.delenv("XDG_DATA_HOME")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert (
        _opencode_db_path()
        == tmp_path / ".local" / "share" / "opencode" / "opencode.db"
    )


def test_flatten_content_string_none_and_empty() -> None:
    assert _flatten_opencode_content("plain") == "plain"
    assert _flatten_opencode_content(None) is None
    assert _flatten_opencode_content([]) is None
    assert _flatten_opencode_content([{"type": "reasoning", "text": "x"}]) is None


def test_timestamp_conversion() -> None:
    assert _opencode_timestamp({"created": 1_793_518_083_819}) == pytest.approx(
        1_793_518_083.819
    )
    assert _opencode_timestamp({"created": True}) is None
    assert _opencode_timestamp({"created": "yesterday"}) is None
    assert _opencode_timestamp(None) is None
