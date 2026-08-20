"""Tests for ``agt task list``.

The Server's ``GET /api/tasks`` already exists; this command simply exposes
it. We mock ``asyncio.run`` (the same pattern used by
``test_agt_task_clean.py``) so the CLI is exercised end-to-end without a
real Server.
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


def _status(
    task_id: str,
    status: str,
    *,
    provider: str = "claude",
    exit_code: int | None = 0,
    duration: float | None = 0.1,
    updated_at: str = "2026-08-19 12:34:56",
) -> dict:
    return {
        "task_id": task_id,
        "provider": provider,
        "status": status,
        "session_id": None,
        "exit_code": exit_code,
        "stdout": "",
        "stderr": "",
        "duration": duration,
        "created_at": "2026-08-19 12:00:00",
        "updated_at": updated_at,
        "daemon_id": "d1",
        "target_daemon_id": None,
        "work_dir": None,
    }


def test_list_renders_table() -> None:
    """Three tasks come back printed as a table with the expected columns."""
    fake = [
        _status("abc12345", "completed", provider="claude", exit_code=0, duration=5.08),
        _status("def67890", "failed", provider="codex", exit_code=1, duration=12.30),
        _status("ghi11111", "running", exit_code=None, duration=None),
    ]
    with patch("agt.cli.main.asyncio.run", return_value=fake):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "list"])
        assert result.exit_code == 0, result.output
        assert "3 task(s)" in result.output
        # Header columns
        assert "TASK_ID" in result.output
        assert "PROVIDER" in result.output
        assert "STATUS" in result.output
        # Each row is present (truncated by display width is fine; the
        # task_ids are 8 chars so they fit in the 10-char column).
        for row in ("abc12345", "def67890", "ghi11111"):
            assert row in result.output
        # Running task has no exit code → em dash sentinel.
        assert "—" in result.output


def test_list_empty() -> None:
    """No tasks on the Server ⇒ friendly message, exit 0."""
    with patch("agt.cli.main.asyncio.run", return_value=[]):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "list"])
        assert result.exit_code == 0
        assert "No tasks" in result.output


def test_list_passes_status_filter() -> None:
    """``--status`` is forwarded to ``ServerClient.list_tasks`` as a list."""
    captured: dict = {}

    from agt.comm.client import ServerClient

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def list_tasks(self, statuses=None, limit=50):
            captured["statuses"] = statuses
            captured["limit"] = limit
            return []

    with patch("agt.cli.main.ServerClient", _StubClient):
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["task", "list", "--status", "completed", "--status", "failed", "--limit", "10"],
        )
        assert result.exit_code == 0, result.output
        assert captured["statuses"] == ["completed", "failed"]
        assert captured["limit"] == 10


def test_list_no_status_filter_passes_none() -> None:
    """When ``--status`` is omitted, the CLI must NOT pass an empty list
    (which would be interpreted as "filter to nothing" by the server)."""
    captured: dict = {}

    from agt.comm.client import ServerClient

    class _StubClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def list_tasks(self, statuses=None, limit=50):
            captured["statuses"] = statuses
            return []

    with patch("agt.cli.main.ServerClient", _StubClient):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "list"])
        assert result.exit_code == 0, result.output
        assert captured["statuses"] is None


def test_list_server_error_exits_nonzero() -> None:
    """A server-side failure surfaces as exit 1 + a clear error."""
    with patch("agt.cli.main.asyncio.run", side_effect=RuntimeError("boom")):
        runner = CliRunner()
        result = runner.invoke(cli, ["task", "list"])
        assert result.exit_code == 1
        assert "boom" in result.output
