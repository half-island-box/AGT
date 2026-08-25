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
from agt.task.session import CwdSessionStore


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
    # agent_cwd defaults to /home/<user>; tests below use tmp_path as
    # cwd (which lives under /tmp), so widen the allowlist to tmp_path's
    # root so the cwd-injection allowlist check doesn't block them.
    cfg.agent_cwd_allowlist = [tmp_path]
    controller = Daemon(config=cfg)
    controller._started = True
    controller._runtimes["claude"] = _runtime()
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
    store = CwdSessionStore.load_or_create(tmp_path / "claude" / "t-cli")
    assert store.is_empty(), "session_id should NOT be written in interactive mode"


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
    store = CwdSessionStore.load_or_create(tmp_path / "claude" / "t-pipe")
    assert store.get("s-xyz") is not None


def test_bare_chat_does_not_create_tasks_subdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``Daemon.run`` called like bare ``agt chat`` (agent_cwd given,
    agt_meta_dir=None) must NOT create ``<agent_cwd>/.agt/tasks/<id>/``.
    That subdirectory was always empty and just accumulated over time.
    """
    async def fake_execute(self, task, *, interactive=False):
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    agent_cwd = tmp_path / "proj"
    agent_cwd.mkdir()

    async def _run() -> None:
        controller = _make_daemon(tmp_path)
        await controller.run(
            provider="claude", prompt="hi", task_id="chat-abcd1234",
            agent_cwd=agent_cwd,
            # agt_meta_dir deliberately omitted (bare-chat path).
        )

    asyncio.run(_run())

    tasks_dir = agent_cwd / ".agt" / "tasks"
    assert not tasks_dir.exists(), (
        f"bare chat must not create {tasks_dir}; "
        f"contents: {sorted(p.name for p in tasks_dir.iterdir()) if tasks_dir.exists() else 'n/a'}"
    )


def test_explicit_work_dir_still_creates_meta_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: ``--work-dir`` / ``--no-work-dir`` overrides still
    create their per-task meta dirs as the user explicitly asked."""
    captured: dict = {}

    async def fake_execute(self, task, *, interactive=False):
        captured["work_dir"] = task.work_dir
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    explicit = tmp_path / "meta"

    async def _run() -> None:
        controller = _make_daemon(tmp_path)
        await controller.run(
            provider="claude", prompt="hi", task_id="t-explicit",
            agent_cwd=tmp_path / "proj",
            agt_meta_dir=explicit,
        )

    asyncio.run(_run())

    assert captured["work_dir"] == explicit, (
        "explicit agt_meta_dir must be wired into task.work_dir"
    )

    # Now exercise the real ``_ensure_work_dir`` to confirm the mkdir
    # path still works for explicit overrides.
    task = Task(
        prompt="hi", provider="claude", task_id="t-explicit",
        work_dir=tmp_path / "meta2",
    )
    executor = TaskExecutor(runtime=_runtime(), config=_config())
    executor._ensure_work_dir(task)
    assert (tmp_path / "meta2").is_dir()


def test_legacy_fallback_when_both_cwds_absent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression: when neither ``agent_cwd`` nor ``agt_meta_dir`` is
    given, the daemon falls back to its ``work_dir_root`` legacy layout
    (e.g. ``~/.agt/workspaces/<provider>/<id>/``)."""
    captured: dict = {}

    async def fake_execute(self, task, *, interactive=False):
        captured["work_dir"] = task.work_dir
        captured["agent_cwd"] = task.agent_cwd
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    async def _run() -> TaskResult:
        controller = _make_daemon(tmp_path)
        return await controller.run(
            provider="claude", prompt="hi", task_id="t-legacy",
        )

    result = asyncio.run(_run())

    assert result.status == "completed"
    # task.work_dir is wired to the legacy work_dir_root path so that
    # ``_ensure_work_dir`` will mkdir it when a real executor runs.
    expected_legacy = tmp_path / "claude" / "t-legacy"
    assert captured["work_dir"] == expected_legacy
    # And confirm the real mkdir path still works for legacy.
    task = Task(
        prompt="hi", provider="claude", task_id="t-legacy2",
        work_dir=tmp_path / "claude" / "t-legacy2",
    )
    executor = TaskExecutor(runtime=_runtime(), config=_config())
    executor._ensure_work_dir(task)
    assert (tmp_path / "claude" / "t-legacy2").is_dir()


def test_bare_chat_no_mkdir_when_work_dir_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When ``task.work_dir`` is ``None`` (bare-chat path), the executor
    must NOT create any directory — ``_ensure_work_dir`` just returns
    ``agent_cwd``. This is the executor-side half of the "no per-task
    subdir" guarantee."""
    agent_cwd = tmp_path / "proj"
    agent_cwd.mkdir()
    task = Task(
        prompt="hi", provider="claude", task_id="t-bare",
        agent_cwd=agent_cwd,
        # work_dir deliberately None.
    )
    executor = TaskExecutor(runtime=_runtime(), config=_config())
    cwd = executor._ensure_work_dir(task)
    assert cwd == agent_cwd
    assert not (agent_cwd / ".agt").exists(), (
        "executor must not create .agt/ under agent_cwd"
    )


def test_ensure_work_dir_creates_agent_cwd_when_missing(tmp_path: Path) -> None:
    """``agent_cwd`` may name a directory the daemon hasn't created yet
    (e.g. ``--dir /root/project`` on a fresh machine). The executor
    creates it on demand so the subprocess spawn + identity injection
    have a real cwd instead of failing."""
    agent_cwd = tmp_path / "does" / "not" / "exist" / "yet"
    task = Task(
        prompt="hi", provider="claude", task_id="t-mkdir",
        agent_cwd=agent_cwd,
    )
    executor = TaskExecutor(runtime=_runtime(), config=_config())
    cwd = executor._ensure_work_dir(task)
    assert cwd == agent_cwd
    assert agent_cwd.is_dir()


def test_ensure_work_dir_permission_error_propagates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If the daemon can't create ``agent_cwd`` (permission denied), the
    OSError propagates so the task fails loudly instead of silently
    landing in the daemon's home dir."""
    agent_cwd = tmp_path / "no" / "perm"

    def _deny(self, *args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", _deny)
    task = Task(
        prompt="hi", provider="claude", task_id="t-perm",
        agent_cwd=agent_cwd,
    )
    executor = TaskExecutor(runtime=_runtime(), config=_config())
    with pytest.raises(PermissionError):
        executor._ensure_work_dir(task)


# ── Daemon.run: identity mirror on chat start ─────────────────────
#
# Phase 4 Step 4 supplement: a remote daemon doesn't have the
# ``~/.agt/identities/<slug>/AGENTS.md`` that ``agt identity create``
# wrote on the CLI machine. ``Daemon.run`` now calls
# ``ensure_identity_md`` to fetch the identity from the Server and
# mirror it locally before injecting — otherwise ``_inject_identity``
# silently no-ops and the agent loses its identity.


def _identity_json(updated_at: str = "2026-08-25 14:23:11") -> dict:
    return {
        "did": "did:agt:code-reviewer",
        "daemon_id": "d-uuid5-form",
        "name": "Code Reviewer",
        "description": "Reviews PRs.",
        "capabilities": ["python", "code-review"],
        "status": "active",
        "visibility": "private",
        "created_by": "alice",
        "created_at": "2026-08-20 10:00:00",
        "updated_at": updated_at,
    }


def test_daemon_run_mirrors_identity_on_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """End-to-end: ``Daemon.run`` with ``identity_did`` triggers
    ``ensure_identity_md``, which writes the AGENTS.md mirror at the
    path ``_inject_identity`` reads from. This is the user-reported
    remote-daemon bug: the file is now present where the inject path
    expects it."""
    # Sandbox ``Path.home()`` so no real ``~/.agt`` is touched.
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    # Stub the HTTP fetch — return a canned identity.
    from agt.daemon import identity_sync as isync

    async def _fake_fetch(**_kw):
        return _identity_json()

    monkeypatch.setattr(isync, "_fetch_identity", _fake_fetch)

    # Stub executor so no agent subprocess actually runs.
    captured: dict = {}

    async def fake_execute(self, task, *, interactive=False):
        captured["identity_did"] = task.identity_did
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    controller = _make_daemon(tmp_path)

    async def _run():
        return await controller.run(
            provider="claude", prompt="hi", task_id="t-idem",
            identity_did="did:agt:code-reviewer",
        )

    asyncio.run(_run())

    # 1. The fetch was called for the right DID.
    # 2. The mirror file now lives at the path ``_inject_identity``
    #    reads from on this machine.
    mirror = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
    assert mirror.exists(), (
        f"AGENTS.md mirror not written at {mirror}; contents of "
        f"{tmp_path / '.agt'}: "
        f"{list((tmp_path / '.agt').rglob('*')) if (tmp_path / '.agt').exists() else 'n/a'}"
    )
    # 3. The mirror contains the rendered identity content.
    body = mirror.read_text(encoding="utf-8")
    assert "did:agt:code-reviewer" in body
    assert "Code Reviewer" in body
    # 4. The cache recorded the updated_at.
    cache = isync.load_state()
    assert cache["identities"]["did:agt:code-reviewer"] == "2026-08-25 14:23:11"
    # 5. The executor saw the identity_did (so inject would have run).
    assert captured["identity_did"] == "did:agt:code-reviewer"


def test_daemon_run_skips_ensure_when_no_identity(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    """Regression: ``Daemon.run`` without ``identity_did`` must not call
    ``ensure_identity_md`` — there's nothing to mirror."""
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    from agt.daemon import identity_sync as isync
    ensure_called = False

    async def _spy(**_kw):
        nonlocal ensure_called
        ensure_called = True
        return None

    monkeypatch.setattr(isync, "ensure_identity_md", _spy)

    async def fake_execute(self, task, *, interactive=False):
        return _fake_result(task.task_id, task.provider, session_id=None)

    monkeypatch.setattr(
        "agt.task.executor.TaskExecutor.execute", fake_execute,
    )

    controller = _make_daemon(tmp_path)

    async def _run():
        await controller.run(
            provider="claude", prompt="hi", task_id="t-noidentity",
        )

    asyncio.run(_run())

    assert not ensure_called, "ensure_identity_md must not run when identity_did is None"