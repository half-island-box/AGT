"""Phase 4 workdir supplement: daemon's ``_execute_one`` forwards ``agent_cwd``.

The Server's claim payload carries the submitter's ``agent_cwd`` — the
directory the agent subprocess should run in on the daemon machine. The
daemon passes it verbatim to ``Daemon.run``; the daemon (not the CLI) then
enforces the allowlist and creates the directory if it's missing. There is
no more ``work_dir``-stripping step here — the legacy ``work_dir`` field is
no longer part of the claim payload (the claim service always sends
``work_dir=None`` and ``agent_cwd=<path-or-None>``).

We exercise the forwarding branch without actually spawning an agent
subprocess — the agent binary is stubbed via ``Daemon.run``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from agt.daemon.main import _execute_one
from agt.task.models import TaskResult


class _StubSemaphore:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _StubClient:
    """Stand-in for ``ServerClient`` — ``_execute_one`` calls report_task."""
    async def report_task(self, **_kwargs) -> bool:
        return True


async def _claim_then_execute(agent_cwd: str | None) -> dict:
    """Run ``_execute_one`` with a controlled payload. Returns the
    kwargs the executor's ``Daemon.run`` was invoked with."""
    captured: dict = {}

    class _CaptureDaemon:
        async def run(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            return TaskResult(
                task_id="abc12345",
                provider="claude",
                exit_code=0,
                stdout="",
                stderr="",
                duration=0.0,
                status="completed",
            )

    payload = {
        "task_id": "abc12345",
        "provider": "claude",
        "prompt": "hi",
        "resume_session_id": None,
    }
    if agent_cwd is not None:
        payload["agent_cwd"] = agent_cwd

    await _execute_one(
        client=_StubClient(),  # type: ignore[arg-type]
        daemon=_CaptureDaemon(),  # type: ignore[arg-type]
        semaphore=_StubSemaphore(),
        shutdown=asyncio.Event(),
        task_payload=payload,
    )
    return captured


def test_agent_cwd_forwarded_to_daemon_run() -> None:
    """Payload ``agent_cwd`` is passed verbatim as ``Daemon.run(agent_cwd=…)``;
    the meta dir is always ``None`` (no per-task bookkeeping dir)."""
    captured = asyncio.run(_claim_then_execute("/srv/proj"))

    assert captured["agent_cwd"] == Path("/srv/proj")
    assert captured["agt_meta_dir"] is None


def test_legacy_task_no_agent_cwd_field() -> None:
    """No ``agent_cwd`` on the payload → daemon sees ``agent_cwd=None``
    and ``agt_meta_dir=None`` and applies its own fallback layout."""
    captured = asyncio.run(_claim_then_execute(None))

    assert captured["agent_cwd"] is None
    assert captured["agt_meta_dir"] is None
