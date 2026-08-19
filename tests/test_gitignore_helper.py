"""Tests for ``_ensure_gitignore`` (Phase 4 cwd-based task workdir).

Idempotently appends ``.agt/`` to ``<cwd>/.gitignore`` when:

- ``<cwd>/.git`` exists (it's a git project)
- the entry isn't already listed (literal, wildcard, or bare token)

When ``<cwd>/.git`` is absent, no file is touched — non-git projects
shouldn't get a .gitignore out of nowhere.
"""

from __future__ import annotations

from pathlib import Path

from agt.cli.main import _ensure_gitignore


def test_no_git_dir_is_noop(tmp_path: Path) -> None:
    _ensure_gitignore(tmp_path)
    assert not (tmp_path / ".gitignore").exists()


def test_appends_entry_to_existing_gitignore(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".gitignore"
    gi.write_text("node_modules/\n")
    _ensure_gitignore(tmp_path)
    text = gi.read_text()
    assert ".agt/" in text
    assert text.startswith("node_modules/\n")


def test_creates_gitignore_when_missing(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    _ensure_gitignore(tmp_path)
    text = (tmp_path / ".gitignore").read_text()
    assert ".agt/" in text


def test_idempotent_when_literal_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".gitignore"
    gi.write_text(".agt/\n")
    _ensure_gitignore(tmp_path)
    # No duplicate appended
    assert gi.read_text().count(".agt/") == 1


def test_idempotent_when_wildcard_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".gitignore"
    gi.write_text(".agt*\n")
    _ensure_gitignore(tmp_path)
    # We treat ``.agt*`` as already covering ``.agt/``; no duplicate line.
    assert gi.read_text() == ".agt*\n"


def test_idempotent_when_bare_token_present(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    gi = tmp_path / ".gitignore"
    gi.write_text(".agt\n")
    _ensure_gitignore(tmp_path)
    assert gi.read_text() == ".agt\n"