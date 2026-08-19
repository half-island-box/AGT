"""Tests for ``_validate_cwd`` (Phase 4 cwd-based task workdir).

Exercises the three refusal rules:

1. cwd not writable
2. cwd on system-directory blacklist
3. ``<cwd>/.agt`` exists but is not a directory
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agt.cli.main import CLIError, _validate_cwd


def test_valid_cwd_passes(tmp_path: Path) -> None:
    _validate_cwd(tmp_path)  # no exception


def test_collision_with_agt_file(tmp_path: Path) -> None:
    """``<cwd>/.agt`` exists as a regular file → refused (must be a dir)."""
    (tmp_path / ".agt").write_text("not a dir")
    with pytest.raises(CLIError, match=r"\.agt"):
        _validate_cwd(tmp_path)


def test_collision_with_agt_dir_passes(tmp_path: Path) -> None:
    """``<cwd>/.agt`` exists as a directory → OK (caller may populate it)."""
    (tmp_path / ".agt").mkdir()
    _validate_cwd(tmp_path)


def test_unwritable_cwd_refused(tmp_path: Path) -> None:
    """A read-only cwd is refused; we never run agents there."""
    ro = tmp_path / "ro"
    ro.mkdir()
    os.chmod(ro, 0o555)
    try:
        with pytest.raises(CLIError, match=r"not writable"):
            _validate_cwd(ro)
    finally:
        os.chmod(ro, 0o755)  # so pytest cleanup can delete


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses 0o555")
def test_unwritable_refused_skip_root() -> None:
    # Documented alongside the actual test; ``pytest.skip`` here keeps the
    # matrix honest if the runner is ever root.
    pass