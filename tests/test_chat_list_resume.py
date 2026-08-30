"""Tests for the ``agt chat`` session list/resume CLI helpers.

Covers the terminal-facing pieces of Phase 4 chat-multisession:
``_humanize_age`` / ``_truncate`` (pure formatters), ``_print_chat_session_list``
(renders the cwd store), and ``_print_resumed_history`` (prints past turns,
or errors on a missing session id).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agt.agent_history import Message
from agt.cli.main import (
    _humanize_age,
    _print_chat_session_list,
    _print_resumed_history,
    _truncate,
)
from agt.task.session import CwdSessionStore


def test_humanize_age_buckets() -> None:
    assert _humanize_age(10) == "just now"
    assert _humanize_age(90) == "1 min ago"
    assert _humanize_age(7200) == "2 hours ago"
    assert _humanize_age(172800) == "2 days ago"


def test_truncate_short_and_long() -> None:
    assert _truncate("short", 60) == "short"
    long = "x" * 100
    out = _truncate(long, 10)
    assert len(out) <= 10
    assert out.endswith("…")


def test_truncate_collapses_newlines() -> None:
    assert _truncate("line one\nline two", 100) == "line one line two"


def test_print_chat_session_list_empty(tmp_path: Path, capsys) -> None:
    _print_chat_session_list(tmp_path / "proj")
    out = capsys.readouterr().out
    assert "No chat sessions" in out


def test_print_chat_session_list_renders_rows(tmp_path: Path, capsys) -> None:
    cwd = tmp_path / "proj"
    store = CwdSessionStore.load_or_create(cwd)
    store.record(session_id="sess-abc", task_id="chat-1", first_prompt="hello world", turn_count=2)
    _print_chat_session_list(cwd)
    out = capsys.readouterr().out
    assert "sess-abc" in out
    assert "hello world" in out
    assert "1 session(s)" in out


def test_print_chat_session_list_shows_provider_column(tmp_path: Path, capsys) -> None:
    cwd = tmp_path / "proj"
    CwdSessionStore.load_or_create(cwd, provider="claude").record(
        session_id="s-claude", task_id="chat-1", first_prompt="a", turn_count=1,
    )
    CwdSessionStore.load_or_create(cwd, provider="codex").record(
        session_id="s-codex", task_id="chat-2", first_prompt="b", turn_count=1,
    )
    _print_chat_session_list(cwd)
    out = capsys.readouterr().out
    assert "PROVIDER" in out
    assert "claude" in out
    assert "codex" in out
    assert "2 session(s)" in out


def test_print_chat_session_list_filters_by_provider(tmp_path: Path, capsys) -> None:
    cwd = tmp_path / "proj"
    CwdSessionStore.load_or_create(cwd, provider="claude").record(
        session_id="s-claude", task_id="chat-1", first_prompt="a", turn_count=1,
    )
    CwdSessionStore.load_or_create(cwd, provider="codex").record(
        session_id="s-codex", task_id="chat-2", first_prompt="b", turn_count=1,
    )
    _print_chat_session_list(cwd, provider="codex")
    out = capsys.readouterr().out
    assert "s-codex" in out
    assert "s-claude" not in out
    assert "1 session(s)" in out


def test_print_chat_session_list_filter_no_match(tmp_path: Path, capsys) -> None:
    cwd = tmp_path / "proj"
    CwdSessionStore.load_or_create(cwd, provider="claude").record(
        session_id="s-claude", task_id="chat-1", first_prompt="a", turn_count=1,
    )
    _print_chat_session_list(cwd, provider="cursor")
    out = capsys.readouterr().out
    assert "No cursor chat sessions" in out


def test_print_resumed_history_missing_session(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _print_resumed_history(tmp_path / "proj", "nope", None, "http://x")
    assert exc_info.value.code == 2
    assert "not found" in capsys.readouterr().err


def test_print_resumed_history_remote_dispatches(
    tmp_path: Path, monkeypatch
) -> None:
    """Remote resume delegates to the Server-backed history fetch."""
    called: dict = {}

    def _fake_remote(session_id, server_url):
        called.update(session_id=session_id, server_url=server_url)

    monkeypatch.setattr("agt.cli.main._print_remote_resumed_history", _fake_remote)
    _print_resumed_history(tmp_path / "proj", "sess-abc", "d1", "http://x")
    assert called == {"session_id": "sess-abc", "server_url": "http://x"}


def test_print_resumed_history_prints_turns(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    cwd = tmp_path / "proj"
    store = CwdSessionStore.load_or_create(cwd)
    store.record(session_id="sess-abc", task_id="chat-1", first_prompt="hi", turn_count=1)

    monkeypatch.setattr(
        "agt.cli.main.load_history",
        lambda provider, session_id: [
            Message(role="user", content="what is 2+2"),
            Message(role="assistant", content="4"),
        ],
    )
    _print_resumed_history(cwd, "sess-abc", None, "http://x")
    out = capsys.readouterr().out
    assert "what is 2+2" in out
    assert "4" in out


def test_print_resumed_history_no_history_available(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    cwd = tmp_path / "proj"
    store = CwdSessionStore.load_or_create(cwd)
    store.record(session_id="sess-abc", task_id="chat-1", first_prompt="hi", turn_count=1)

    monkeypatch.setattr("agt.cli.main.load_history", lambda provider, session_id: [])
    _print_resumed_history(cwd, "sess-abc", None, "http://x")
    assert "no history available" in capsys.readouterr().err
