"""Tests for ``CwdSessionStore`` (Phase 4 chat-multisession).

The store is the append-only, cwd-scoped index behind ``agt chat --list``
and ``--resume``. These tests pin the append/update/list/delete semantics,
disk round-trip, newest-first ordering, and legacy-format migration.
"""

from __future__ import annotations

import json
from pathlib import Path

from agt.task.session import CwdSessionStore, SessionEntry


def _store(tmp_path: Path) -> CwdSessionStore:
    return CwdSessionStore.load_or_create(tmp_path / "proj")


def test_record_adds_entry(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="chat-aaaa", first_prompt="hi", turn_count=1)
    assert len(store.list_sessions()) == 1
    entry = store.get("s1")
    assert entry is not None
    assert entry.session_id == "s1"
    assert entry.task_id == "chat-aaaa"
    assert entry.turn_count == 1
    assert entry.first_prompt == "hi"


def test_record_update_preserves_first_prompt(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="chat-aaaa", first_prompt="first", turn_count=1)
    store.record(session_id="s1", task_id="chat-bbbb", first_prompt="ignored", turn_count=3)
    entries = store.list_sessions()
    assert len(entries) == 1  # no duplicate entry
    entry = entries[0]
    assert entry.task_id == "chat-bbbb"  # task_id tracks the latest
    assert entry.turn_count == 3
    assert entry.first_prompt == "first"  # first_prompt is immutable


def test_list_newest_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="older", task_id="t1", first_prompt="a", turn_count=1)
    store.record(session_id="newer", task_id="t2", first_prompt="b", turn_count=1)
    assert [e.session_id for e in store.list_sessions()] == ["newer", "older"]


def test_last_returns_most_recent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.last() is None
    store.record(session_id="a", task_id="t1", first_prompt="x", turn_count=1)
    store.record(session_id="b", task_id="t2", first_prompt="y", turn_count=1)
    assert store.last() is not None
    assert store.last().session_id == "b"


def test_delete_removes_and_persists(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="t1", first_prompt="x", turn_count=1)
    store.record(session_id="s2", task_id="t2", first_prompt="y", turn_count=1)
    assert store.delete("s1") is True
    assert store.delete("missing") is False
    assert store.get("s1") is None
    assert store.get("s2") is not None
    # Reload from disk confirms the delete was persisted.
    reloaded = CwdSessionStore.load(tmp_path / "proj")
    assert [e.session_id for e in reloaded.list_sessions()] == ["s2"]


def test_is_empty(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.is_empty() is True
    store.record(session_id="s1", task_id="t1", first_prompt="x", turn_count=1)
    assert store.is_empty() is False


def test_persistence_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.record(session_id="s1", task_id="t1", first_prompt="hello", turn_count=2)
    reloaded = CwdSessionStore.load(tmp_path / "proj")
    entry = reloaded.get("s1")
    assert entry is not None
    assert entry.first_prompt == "hello"
    assert entry.turn_count == 2
    # File layout: dict with cwd/provider/sessions keys.
    raw = json.loads((tmp_path / "proj" / ".agt" / "sessions.json").read_text())
    assert set(raw.keys()) == {"cwd", "provider", "sessions"}
    assert isinstance(raw["sessions"], list)


def test_entry_provider_round_trips_on_reload(tmp_path: Path) -> None:
    """A session's provider is stored per entry and survives a reload —
    the CLI loads stores with the default ``provider="claude"`` but a
    codex entry must still report ``codex`` so ``--list``/``--resume``
    pick the right provider."""
    store = CwdSessionStore.load_or_create(tmp_path / "proj", provider="codex")
    store.record(session_id="s1", task_id="t1", first_prompt="hi", turn_count=1)
    # Reload WITHOUT passing provider (mimics _print_chat_session_list /
    # _print_resumed_history, which call load_or_create(cwd) with the default).
    reloaded = CwdSessionStore.load_or_create(tmp_path / "proj")
    entry = reloaded.get("s1")
    assert entry is not None
    assert entry.provider == "codex"


def test_record_stamps_current_provider(tmp_path: Path) -> None:
    """New entries are tagged with the store's provider; different
    providers coexist in the same cwd store."""
    claude = CwdSessionStore.load_or_create(tmp_path / "proj", provider="claude")
    claude.record(session_id="s-claude", task_id="t1", first_prompt="a", turn_count=1)
    codex = CwdSessionStore.load_or_create(tmp_path / "proj", provider="codex")
    codex.record(session_id="s-codex", task_id="t2", first_prompt="b", turn_count=1)

    reloaded = CwdSessionStore.load_or_create(tmp_path / "proj")
    assert reloaded.get("s-claude").provider == "claude"
    assert reloaded.get("s-codex").provider == "codex"


def test_load_or_create_missing_file(tmp_path: Path) -> None:
    store = CwdSessionStore.load_or_create(tmp_path / "nonexistent")
    assert store.is_empty() is True
    assert store.cwd == tmp_path / "nonexistent"


def test_load_or_create_corrupt_file(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    (cwd / ".agt").mkdir(parents=True)
    (cwd / ".agt" / "sessions.json").write_text("{not valid json", encoding="utf-8")
    store = CwdSessionStore.load_or_create(cwd)
    assert store.is_empty() is True


def test_legacy_migration_rewrites_to_array(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    (cwd / ".agt").mkdir(parents=True)
    legacy = {
        "records": {
            "chat-aaaa": {
                "session_id": "s1",
                "provider": "claude",
                "created_at": 1724000000.0,
            },
        }
    }
    (cwd / ".agt" / "sessions.json").write_text(json.dumps(legacy), encoding="utf-8")

    store = CwdSessionStore.load(cwd)
    entries = store.list_sessions()
    assert len(entries) == 1
    entry = entries[0]
    assert entry.session_id == "s1"
    assert entry.task_id == "chat-aaaa"
    assert entry.turn_count == 0
    # Migrated shape persisted, and a .bak snapshot written.
    raw = json.loads((cwd / ".agt" / "sessions.json").read_text())
    assert "records" not in raw
    assert "sessions" in raw
    assert (cwd / ".agt" / "sessions.json.bak").exists()


def test_legacy_migration_preserves_per_record_provider(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    (cwd / ".agt").mkdir(parents=True)
    legacy = {
        "records": {
            "chat-aaaa": {"session_id": "s1", "provider": "codex"},
            "chat-bbbb": {"session_id": "s2", "provider": "claude"},
        }
    }
    (cwd / ".agt" / "sessions.json").write_text(json.dumps(legacy), encoding="utf-8")
    store = CwdSessionStore.load(cwd, provider="claude")
    by_id = {e.session_id: e.provider for e in store.list_sessions()}
    # Both providers survive migration (multi-provider store) with their
    # per-record provider tagged.
    assert by_id == {"s1": "codex", "s2": "claude"}


def test_cleanup_legacy_backup(tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    (cwd / ".agt").mkdir(parents=True)
    bak = cwd / ".agt" / "sessions.json.bak"
    bak.write_text("{}", encoding="utf-8")
    store = CwdSessionStore.load_or_create(cwd)
    assert store.cleanup_legacy_backup() is True
    assert not bak.exists()
    assert store.cleanup_legacy_backup() is False


def test_entry_as_dict_round_trip() -> None:
    e = SessionEntry(
        session_id="s1",
        first_seen_at=1.0,
        last_seen_at=2.0,
        task_id="t1",
        turn_count=3,
        first_prompt="hi",
    )
    d = e.as_dict()
    assert d["session_id"] == "s1"
    assert d["first_prompt"] == "hi"
