"""Tests for Pi session probing/deletion (``agt/agent_history.py``).

Pi sessions are individual JSONL files, so the generic unlink path is
safe. The header ``id`` — not the filename — is authoritative, which
keeps a custom ``--session-id`` containing underscores from matching a
different file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agt.agent_history import (
    delete_provider_session,
    find_pi_session_files,
    find_provider_session_files,
)

SESSION_ID = "01a19656-4979-74f6-b563-a8bbd980780b"


def _session_file(
    root: Path,
    project: str,
    *,
    session_id: str = SESSION_ID,
    filename: str | None = None,
    content: str = "hi",
) -> Path:
    directory = root / project
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename or f"2026-11-01T00-00-00-000Z_{session_id}.jsonl")
    header = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": "2026-11-01T00:00:00.000Z",
        "cwd": "/private/tmp/project",
    }
    message = {
        "type": "message",
        "id": "u1",
        "parentId": None,
        "timestamp": "2026-11-01T00:00:01.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": content}],
        },
    }
    path.write_text(
        json.dumps(header) + "\n" + json.dumps(message) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture()
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setenv("AGT_PI_SESSION_DIR", str(root))
    return root


def test_find_returns_every_matching_file(sessions_root: Path) -> None:
    first = _session_file(sessions_root, "--project-a--")
    second = _session_file(sessions_root, "--project-b--")

    assert set(find_pi_session_files(SESSION_ID)) == {first, second}
    assert set(find_provider_session_files("pi", SESSION_ID)) == {
        first,
        second,
    }


def test_find_ignores_filename_match_with_wrong_header(
    sessions_root: Path,
) -> None:
    _session_file(
        sessions_root,
        "--project-a--",
        session_id="different",
        filename=f"2026-11-01T00-00-00-000Z_{SESSION_ID}.jsonl",
    )
    assert find_pi_session_files(SESSION_ID) == []


def test_delete_removes_all_matching_files(sessions_root: Path) -> None:
    first = _session_file(sessions_root, "--project-a--")
    second = _session_file(sessions_root, "--project-b--")
    other = _session_file(sessions_root, "--project-c--", session_id="other")

    result = delete_provider_session("pi", SESSION_ID)

    assert result.supported is True
    assert result.missing is False
    assert result.errors == ()
    assert set(result.deleted_paths) == {first, second}
    assert not first.exists()
    assert not second.exists()
    assert other.exists()


def test_delete_unknown_session_reports_missing(sessions_root: Path) -> None:
    _session_file(sessions_root, "--project-a--", session_id="other")

    result = delete_provider_session("pi", "unknown-session")

    assert result.supported is True
    assert result.missing is True
    assert result.deleted_paths == ()
    assert result.errors == ()


def test_delete_rejects_unsafe_session_id(sessions_root: Path) -> None:
    with pytest.raises(ValueError):
        delete_provider_session("pi", "../escape")
