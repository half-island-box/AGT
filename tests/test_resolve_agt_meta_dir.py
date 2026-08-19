"""Tests for ``_resolve_agt_meta_dir`` (Phase 4 cwd-based task workdir).

Pure function: priority is ``explicit`` > ``legacy_root`` > ``<cwd>/.agt/tasks/<id>/``.
Absolute-path normalization is part of the contract because the Server
persists the value verbatim.
"""

from __future__ import annotations

from pathlib import Path

from agt.cli.main import _resolve_agt_meta_dir


def _cwd(tmp_path: Path) -> Path:
    return tmp_path / "project-x"


def test_cwd_derivation_default(tmp_path: Path) -> None:
    """No ``--work-dir``, no ``--no-work-dir`` → ``<cwd>/.agt/tasks/<id>/``."""
    cwd = _cwd(tmp_path)
    cwd.mkdir()
    out = _resolve_agt_meta_dir(cwd, "abc12345", explicit=None)
    assert out == (cwd / ".agt" / "tasks" / "abc12345").resolve()


def test_explicit_wins_over_legacy(tmp_path: Path) -> None:
    """``--work-dir`` always wins, even when ``--no-work-dir`` is implied
    via ``legacy_root``."""
    cwd = _cwd(tmp_path)
    cwd.mkdir()
    custom = tmp_path / "elsewhere" / "meta"
    out = _resolve_agt_meta_dir(
        cwd, "x", explicit=custom, legacy_root=cwd,
    )
    assert out == custom.resolve()


def test_legacy_root_fallback(tmp_path: Path) -> None:
    cwd = _cwd(tmp_path)
    cwd.mkdir()
    out = _resolve_agt_meta_dir(cwd, "x", explicit=None, legacy_root=cwd)
    assert out == cwd


def test_explicit_path_is_resolved(tmp_path: Path) -> None:
    """A relative ``--work-dir`` argument is normalized to absolute."""
    cwd = _cwd(tmp_path)
    cwd.mkdir()
    # Build a relative-style argument by passing a non-absolute Path.
    rel = Path(".") / ".." / "var" / "tmp" / "meta"
    out = _resolve_agt_meta_dir(cwd, "x", explicit=rel)
    assert out.is_absolute()
    # The path resolves through the leading .. but is otherwise consistent.
    assert out == rel.resolve()


def test_cwd_derivation_normalizes_existing_symlinks(tmp_path: Path) -> None:
    """``resolve()`` follows symlinks so the persisted path matches what
    the agent subprocess sees."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    out = _resolve_agt_meta_dir(link, "id", explicit=None)
    assert out == (real / ".agt" / "tasks" / "id")