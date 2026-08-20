"""Tests for ``Daemon``'s session resume/record helpers.

Phase 4 chat-multisession pinned the two clarified behaviors:
- bare ``agt chat`` (turn 1, no ``--resume``) starts a **new** session;
- turns after the first continue the session created by the prior turn;
- an explicit ``--resume <sid>`` always wins.
"""

from __future__ import annotations

from pathlib import Path

from agt.daemon.daemon import Daemon
from agt.task.models import TaskResult
from agt.task.session import CwdSessionStore


def _store(tmp_path: Path) -> CwdSessionStore:
    return CwdSessionStore.load_or_create(tmp_path / "proj")


def _result(session_id: str | None = "s-new", resume_rejected: bool = False) -> TaskResult:
    return TaskResult(
        task_id="t",
        provider="claude",
        exit_code=0,
        stdout="",
        stderr="",
        duration=0.1,
        status="completed",
        session_id=session_id,
        resume_rejected=resume_rejected,
    )


def test_explicit_resume_wins_on_first_turn(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s-old", task_id="t-old", first_prompt="x", turn_count=1)
    out = Daemon._resolve_resume_session_id(store, "s-old", interactive=False, turn_index=1)
    assert out == "s-old"


def test_bare_chat_first_turn_is_new_session(tmp_path: Path) -> None:
    """No --resume, turn 1 → None (fresh session), even if a prior
    session exists in this cwd."""
    store = _store(tmp_path)
    store.record(session_id="s-old", task_id="t-old", first_prompt="x", turn_count=1)
    out = Daemon._resolve_resume_session_id(store, None, interactive=False, turn_index=1)
    assert out is None


def test_subsequent_turn_resumes_last(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="t1", first_prompt="a", turn_count=1)
    out = Daemon._resolve_resume_session_id(store, None, interactive=False, turn_index=2)
    assert out == "s1"


def test_subsequent_turn_empty_store_is_none(tmp_path: Path) -> None:
    store = _store(tmp_path)
    out = Daemon._resolve_resume_session_id(store, None, interactive=False, turn_index=2)
    assert out is None


def test_interactive_never_resumes(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="t1", first_prompt="a", turn_count=1)
    out = Daemon._resolve_resume_session_id(store, None, interactive=True, turn_index=2)
    assert out is None


def test_record_outcome_adds_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    Daemon._record_session_outcome(
        store,
        task_id="t1",
        prompt="hello",
        first_prompt=None,
        turn_index=1,
        resume_session_id=None,
        result=_result("s-new"),
    )
    entry = store.get("s-new")
    assert entry is not None
    # first_prompt falls back to prompt when the caller didn't pass one.
    assert entry.first_prompt == "hello"
    assert entry.turn_count == 1


def test_record_outcome_rejected_deletes_pointer(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s-bad", task_id="t1", first_prompt="x", turn_count=1)
    Daemon._record_session_outcome(
        store,
        task_id="t1",
        prompt="hello",
        first_prompt=None,
        turn_index=2,
        resume_session_id="s-bad",
        result=_result(None, resume_rejected=True),
    )
    assert store.get("s-bad") is None
