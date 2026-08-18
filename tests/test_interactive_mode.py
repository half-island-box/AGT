"""Interactive mode tests for ``TaskExecutor.execute`` and ``Daemon.run``.

The CLI path (``agt run`` / ``agt chat``) passes ``interactive=True`` so
the agent CLI sees the parent's TTY — some CLIs (e.g. ``codex``)
refuse to run with stdout=PIPE. The daemon path keeps the default
``interactive=False`` (PIPE) so it can capture output and extract a
session id.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agt.daemon.config import DaemonConfig
from agt.daemon.daemon import Daemon
from agt.runtime.models import AgentEntry, Runtime
from agt.task.executor import TaskExecutor
from agt.task.models import Task, TaskResult
from agt.task.session import SessionStore


SECRET = "interactive-test-secret"


def _runtime() -> Runtime:
    entry = AgentEntry(
        command="cat",
        path=Path("/bin/cat"),
        version="1.0.0",
        provider="claude",
        display_name="Claude",
    )
    return Runtime(
        name="Claude (test)",
        type="claude",
        version="1.0.0",
        status="online",
        agent_entry=entry,
        device_name="test",
    )


def _task(tmp_path: Path) -> Task:
    return Task(
        prompt="hello",
        provider="claude",
        task_id="t-interactive",
        work_dir=tmp_path,
    )


def _config() -> DaemonConfig:
    cfg = DaemonConfig.__new__(DaemonConfig)
    cfg.agent_timeout = 30
    return cfg


def _fake_result(task_id: str, provider: str, *, session_id: str | None) -> TaskResult:
    return TaskResult(
        task_id=task_id,
        provider=provider,
        exit_code=0,
        stdout="",
        stderr="",
        duration=0.1,
        status="completed",
        session_id=session_id,
        resume_rejected=False,
    )


class _FakeProc:
    """Stands in for ``asyncio.subprocess.Process`` in mock tests.

    Pipe-mode tests also need ``stdout`` and ``stderr`` because the
    executor passes them to ``StreamReader`` even though we never read.
    """

    def __init__(self, exit_code: int = 0) -> None:
        self.returncode = exit_code
        self.stdout = None
        self.stderr = None

    async def wait(self) -> int:
        return self.returncode

    async def kill(self) -> None:
        pass


# ── TaskExecutor: interactive plumbing ──────────────────────────


def test_task_executor_interactive_inherits_parent_stdio(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``interactive=True`` must pass ``stdin=stdout=stderr=None`` so the
    subprocess inherits the parent's stdio (the TTY)."""
    import asyncio as _asyncio

    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    async def _run() -> None:
        executor = TaskExecutor(runtime=_runtime(), config=_config())
        result = await executor.execute(_task(tmp_path), interactive=True)
        assert result.status == "completed"
        assert result.session_id is None
        assert result.stdout == ""

    asyncio.run(_run())

    # In interactive mode the agent must see the parent's TTY (None
    # means "inherit parent's fd").
    assert captured.get("stdin") is None
    assert captured.get("stdout") is None
    assert captured.get("stderr") is None


def test_task_executor_pipe_mode_captures_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``interactive=False`` (default) must pipe stdout/stderr so the
    agent's output can be captured for session-id extraction."""
    import asyncio as _asyncio

    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(_asyncio, "create_subprocess_exec", fake_exec)

    async def _run() -> None:
        executor = TaskExecutor(runtime=_runtime(), config=_config())
        await executor.execute(_task(tmp_path))

    asyncio.run(_run())

    # In pipe mode stdout/stderr must be PIPE for output capture.
    assert captured.get("stdout") is _asyncio.subprocess.PIPE
    assert captured.get("stderr") is _asyncio.subprocess.PIPE


# ── Daemon.run: interactive threading ────────────────────────────


def _make_daemon(tmp_path: Path) -> Daemon:
    """Build a Daemon with the probe map pre-seeded."""
    cfg = _config()
    cfg.server_base_url = "http://127.0.0.1:8420"
    cfg.server_secret = SECRET
    cfg.work_dir_root = tmp_path
    controller = Daemon(config=cfg)
    controller._started = True
    controller._runtimes["claude"] = _runtime()
    controller.session_store = SessionStore(tmp_path / "sessions.json")
    return controller


def test_daemon_run_threads_interactive_to_executor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Daemon.run(interactive=True)`` passes ``interactive=True`` down
    to TaskExecutor.execute, and skips the SessionStore write (no
    session_id was captured in interactive mode)."""
    captured: dict = {}

    async def fake_execute(self, task, *, interactive=False):
        captured["interactive"] = interactive
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    controller = _make_daemon(tmp_path)

    async def _run() -> None:
        await controller.run(
            provider="claude", prompt="hi", task_id="t-cli",
            interactive=True,
        )

    asyncio.run(_run())

    assert captured["interactive"] is True
    saved = controller.session_store.get("t-cli", "claude")
    assert saved is None, "session_id should NOT be written in interactive mode"


def test_daemon_run_pipe_mode_writes_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Daemon.run(interactive=False)`` (default) captures session id
    and writes it to SessionStore — preserves existing behavior."""
    captured: dict = {}

    async def fake_execute(self, task, *, interactive=False):
        captured["interactive"] = interactive
        return _fake_result(task.task_id, task.provider, session_id="s-xyz")

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    controller = _make_daemon(tmp_path)

    async def _run() -> None:
        await controller.run(
            provider="claude", prompt="hi", task_id="t-pipe",
        )

    asyncio.run(_run())

    assert captured["interactive"] is False
    saved = controller.session_store.get("t-pipe", "claude")
    assert saved == "s-xyz"