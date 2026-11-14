"""Daemon-side collection of provider-native chat titles."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agt.daemon.config import DaemonConfig
from agt.daemon.daemon import Daemon
from agt.task.models import TaskResult
from agt.task.session import CwdSessionStore


class _Probe:
    def scan(self) -> list:
        return []


class _Executor:
    async def execute(self, task, **_: object) -> TaskResult:
        return TaskResult(
            task_id=task.task_id,
            provider=task.provider,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration=0.1,
            status="completed",
            session_id="sess-title",
        )


def test_daemon_collects_and_persists_provider_title(
    tmp_path: Path,
    monkeypatch,
) -> None:
    daemon = Daemon(
        config=DaemonConfig(agent_cwd_allowlist=[tmp_path]),
        probe=_Probe(),  # type: ignore[arg-type]
    )
    daemon._runtimes["claude"] = object()
    monkeypatch.setattr("agt.daemon.daemon.TaskExecutor", lambda **_: _Executor())
    monkeypatch.setattr(
        "agt.daemon.daemon.load_session_title",
        lambda provider, session_id: "Native title",
    )

    async def _run() -> None:
        result = await daemon.run(
            provider="claude",
            prompt="long first prompt",
            task_id="chat-title",
            agent_cwd=tmp_path,
            first_prompt="long first prompt",
            use_cwd_session_store=True,
        )
        assert result.session_title == "Native title"

    asyncio.run(_run())

    entry = CwdSessionStore.load_or_create(tmp_path, provider="claude").last()
    assert entry is not None
    assert entry.title == "Native title"
    assert entry.first_prompt == "long first prompt"
