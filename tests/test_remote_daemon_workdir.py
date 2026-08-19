"""Phase 4 workdir: daemon's ``_execute_one`` resolves submitted paths.

When the Server hands a task with a ``work_dir`` to the daemon, the
daemon must:

- recover the agent cwd by stripping ``.agt/tasks/<id>`` from the path;
- fall back to the legacy layout when the cwd doesn't exist locally;
- still work for legacy tasks (no ``work_dir`` → daemon fallback).

We exercise the path-resolution branch without actually spawning an
agent subprocess — the agent binary is stubbed via ``Daemon.run``.
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


def _build_meta_path(cwd: Path, task_id: str) -> str:
    return str(cwd / ".agt" / "tasks" / task_id)


async def _claim_then_execute(meta_path: str | None, *, cwd_root: Path | None) -> dict:
    """Run ``_execute_one`` with a controlled payload. Returns the
    kwargs the executor's ``Daemon.run`` was invoked with."""
    captured: dict = {}

    class _CaptureDaemon:
        async def run(self, **kwargs):  # noqa: ANN003
            captured.update(kwargs)
            captured["work_dir_set"] = kwargs.get("agt_meta_dir") is not None
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
    if meta_path is not None:
        payload["work_dir"] = meta_path

    await _execute_one(
        client=_StubClient(),  # type: ignore[arg-type]
        daemon=_CaptureDaemon(),  # type: ignore[arg-type]
        semaphore=_StubSemaphore(),
        shutdown=asyncio.Event(),
        task_payload=payload,
    )
    return captured


def test_cwd_recovered_from_meta_dir(tmp_path: Path) -> None:
    """``<cwd>/.agt/tasks/<id>`` ⇒ agent_cwd = ``<cwd>``."""
    cwd = tmp_path / "project"
    cwd.mkdir()
    meta = _build_meta_path(cwd, "abc12345")

    captured = asyncio.run(_claim_then_execute(meta, cwd_root=cwd))
    assert captured["work_dir_set"] is True
    assert captured["agent_cwd"] == cwd
    assert captured["agt_meta_dir"] == Path(meta)


def test_falls_back_when_cwd_missing(tmp_path: Path) -> None:
    """The submitter's ``<cwd>`` doesn't exist on this daemon — agent_cwd
    is reset to ``None`` so the daemon uses the legacy fallback."""
    nonexistent = tmp_path / "elsewhere" / "user"
    meta = _build_meta_path(nonexistent, "abc12345")

    captured = asyncio.run(_claim_then_execute(meta, cwd_root=nonexistent))
    # Both reset → daemon fallback (work_dir=None passed through)
    assert captured["agent_cwd"] is None
    assert captured["agt_meta_dir"] is None


def test_legacy_task_no_work_dir_field() -> None:
    """No ``work_dir`` on the payload → daemon sees ``agent_cwd=None``
    and ``agt_meta_dir=None`` and applies its own fallback path."""
    captured = asyncio.run(_claim_then_execute(None, cwd_root=None))
    assert captured["agent_cwd"] is None
    assert captured["agt_meta_dir"] is None


def test_custom_work_dir_treated_as_meta(tmp_path: Path) -> None:
    """A non-standard path (``--work-dir`` override) doesn't fit the
    ``.agt/tasks/<id>`` shape; we conservatively use it as the meta dir
    and the same path as the agent cwd."""
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    captured = asyncio.run(_claim_then_execute(str(cwd), cwd_root=cwd))
    assert captured["agt_meta_dir"] == cwd
    # agent_cwd falls back to meta_dir when shape doesn't match
    assert captured["agent_cwd"] == cwd