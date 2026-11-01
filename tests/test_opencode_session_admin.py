"""Tests for OpenCode session probing/deletion (``agt/agent_history.py``).

OpenCode stores every session in one SQLite database, so probing is a
read-only row lookup and deletion goes through the ``opencode`` CLI rather
than unlinking a per-session file. These tests pin both behaviours,
including the guard that keeps the generic unlink path away from the
database file.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest

from agt.agent_history import (
    delete_opencode_session,
    delete_provider_session,
    find_opencode_session_files,
    find_provider_session_files,
)

SESSION_ID = "ses_e69fde1e8ffe76XWze9fJXAuJ7"
BINARY = "/fake/bin/opencode"


def _create_db(path: Path, session_ids: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE session_v2 (id TEXT PRIMARY KEY, parent_id TEXT)"
        )
        connection.executemany(
            "INSERT INTO session_v2 (id) VALUES (?)",
            [(session_id,) for session_id in session_ids],
        )
        connection.commit()
    finally:
        connection.close()
    return path


def _use_db(
    monkeypatch: pytest.MonkeyPatch,
    db: Path,
    *,
    binary: str | None = BINARY,
) -> None:
    monkeypatch.setenv("AGT_OPENCODE_DB", str(db))
    if binary is None:
        monkeypatch.delenv("AGT_OPENCODE_BIN", raising=False)
    else:
        monkeypatch.setenv("AGT_OPENCODE_BIN", binary)


def test_find_returns_db_when_session_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)

    assert find_opencode_session_files(SESSION_ID) == [db]
    assert find_provider_session_files("opencode", SESSION_ID) == [db]


def test_find_returns_empty_for_unknown_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)

    assert find_opencode_session_files("ses_unknown") == []


def test_find_returns_empty_for_missing_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, tmp_path / "absent.db")

    assert find_opencode_session_files(SESSION_ID) == []


def test_find_returns_empty_for_missing_table(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "opencode.db"
    sqlite3.connect(db).close()
    _use_db(monkeypatch, db)

    assert find_opencode_session_files(SESSION_ID) == []


def test_find_rejects_unsafe_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _create_db(tmp_path / "opencode.db", []))

    with pytest.raises(ValueError):
        find_opencode_session_files("../escape")


def test_unknown_provider_still_not_implemented() -> None:
    with pytest.raises(NotImplementedError):
        find_provider_session_files("cursor", SESSION_ID)
    result = delete_provider_session("cursor", SESSION_ID)
    assert result.supported is False
    assert "cursor" in (result.reason or "")


def test_delete_unknown_session_skips_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agt.agent_history.subprocess.run",
        lambda argv, **kwargs: calls.append(argv),
    )

    result = delete_opencode_session("ses_unknown")

    assert result.supported is True
    assert result.missing is True
    assert calls == []


def test_delete_missing_db_skips_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, tmp_path / "absent.db")
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "agt.agent_history.subprocess.run",
        lambda argv, **kwargs: calls.append(argv),
    )

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert result.missing is True
    assert calls == []


def test_delete_success_uses_standalone_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout=f"Session {SESSION_ID} deleted\n", stderr=""
        )

    monkeypatch.setattr("agt.agent_history.subprocess.run", fake_run)

    result = delete_provider_session("opencode", SESSION_ID)

    assert result.supported is True
    assert result.missing is False
    assert result.errors == ()
    assert calls == [[BINARY, "--standalone", "session", "delete", SESSION_ID]]
    # The generic delete path must never unlink OpenCode's shared database.
    assert db.exists()


def test_delete_binary_missing_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db, binary=None)
    monkeypatch.setattr("agt.agent_history.shutil.which", lambda name: None)

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert result.missing is False
    assert len(result.errors) == 1
    assert "AGT_OPENCODE_BIN" in result.errors[0]


def test_delete_reports_cli_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr("agt.agent_history.subprocess.run", fake_run)

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert result.missing is False
    assert len(result.errors) == 1
    assert "exit 1" in result.errors[0]
    assert "boom" in result.errors[0]


def test_delete_session_not_found_output_marks_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(
            argv, 1, stdout="", stderr=f"Session not found: {SESSION_ID}"
        )

    monkeypatch.setattr("agt.agent_history.subprocess.run", fake_run)

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert result.missing is True
    assert result.errors == ()


def test_delete_timeout_reports_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = _create_db(tmp_path / "opencode.db", [SESSION_ID])
    _use_db(monkeypatch, db)

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(argv, 30.0)

    monkeypatch.setattr("agt.agent_history.subprocess.run", fake_run)

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert len(result.errors) == 1
    assert "timed out" in result.errors[0]


def test_delete_proceeds_when_existence_probe_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = tmp_path / "opencode.db"
    db.write_bytes(b"not a sqlite database")
    _use_db(monkeypatch, db)

    def fake_run(argv: list[str], **kwargs) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr("agt.agent_history.subprocess.run", fake_run)

    result = delete_opencode_session(SESSION_ID)

    assert result.supported is True
    assert result.errors == ()


def test_delete_rejects_option_like_session_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _use_db(monkeypatch, _create_db(tmp_path / "opencode.db", [SESSION_ID]))

    with pytest.raises(ValueError):
        delete_opencode_session("--help")
