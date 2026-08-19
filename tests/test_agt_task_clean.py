"""Phase 4 workdir: ``agt task clean`` removes a task's local AGT metadata.

Behaviour:

- Local task with a real ``work_dir`` ⇒ ``shutil.rmtree`` it.
- Remote daemon task (no ``work_dir``) ⇒ warn and exit 4.
- Running task ⇒ refuse without ``--force``.
- ``--all-done`` iterates completed/failed/timeout tasks.
- Missing directory is treated as "already done" (not a failure).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


def _status(task_id: str, status: str, work_dir: str | None) -> dict:
    return {
        "task_id": task_id,
        "provider": "claude",
        "status": status,
        "session_id": None,
        "exit_code": 0,
        "stdout": "",
        "stderr": "",
        "duration": 0.1,
        "created_at": "2026-08-19 00:00:00",
        "updated_at": "2026-08-19 00:00:01",
        "daemon_id": "d1",
        "target_daemon_id": None,
        "work_dir": work_dir,
    }


def test_local_task_is_removed(tmp_path: Path) -> None:
    meta = tmp_path / "project" / ".agt" / "tasks" / "abc12345"
    meta.mkdir(parents=True)
    (meta / "sessions.json").write_text("{}")

    with patch(
        "agt.cli.main.asyncio.run",
        return_value=[_status("abc12345", "completed", str(meta))],
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "abc12345"])
        assert result.exit_code == 0, result.output
        assert not meta.exists()


def test_running_task_refused_without_force(tmp_path: Path) -> None:
    meta = tmp_path / "project" / ".agt" / "tasks" / "abc12345"
    meta.mkdir(parents=True)

    with patch(
        "agt.cli.main.asyncio.run",
        return_value=[_status("abc12345", "running", str(meta))],
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "abc12345"])
        assert result.exit_code == 4
        assert meta.exists(), "directory must not be touched"


def test_running_task_removed_with_force(tmp_path: Path) -> None:
    meta = tmp_path / "project" / ".agt" / "tasks" / "abc12345"
    meta.mkdir(parents=True)

    with patch(
        "agt.cli.main.asyncio.run",
        return_value=[_status("abc12345", "running", str(meta))],
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "abc12345", "--force"])
        assert result.exit_code == 0
        assert not meta.exists()


def test_remote_task_warns_and_skips(tmp_path: Path) -> None:
    """No ``work_dir`` (remote daemon) — warn + exit 4, do not touch any path."""
    with patch(
        "agt.cli.main.asyncio.run",
        return_value=[_status("abc12345", "completed", None)],
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "abc12345"])
        assert result.exit_code == 4
        assert "no work_dir" in result.output


def test_already_missing_is_not_a_failure(tmp_path: Path) -> None:
    """Server still remembers the path but the directory is gone — OK."""
    meta = tmp_path / "project" / ".agt" / "tasks" / "abc12345"
    with patch(
        "agt.cli.main.asyncio.run",
        return_value=[_status("abc12345", "completed", str(meta))],
    ):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "abc12345"])
        assert result.exit_code == 0


def test_all_done_iterates_batch(tmp_path: Path) -> None:
    a = tmp_path / "a" / ".agt" / "tasks" / "id-a"
    b = tmp_path / "b" / ".agt" / "tasks" / "id-b"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    fake_response = [
        _status("id-a", "completed", str(a)),
        _status("id-b", "failed", str(b)),
    ]
    with patch("agt.cli.main.asyncio.run", return_value=fake_response):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "clean", "--all-done"])
        assert result.exit_code == 0
        assert not a.exists()
        assert not b.exists()


def test_no_args_errors() -> None:
    """``agt task clean`` with no TASK_ID and no ``--all-done`` is a usage error."""
    runner = CliRunner()
    result = runner.invoke(cli, ["task", "clean"])
    assert result.exit_code == 2