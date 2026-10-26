"""End-to-end test: Daemon spawns an agent subprocess with AGT_TASK_TOKEN injected.

Verifies the full Phase 3 token flow:

  Daemon (with AGT_SERVER_SECRET)
        │
        │ TaskExecutor._build_env()
        ▼
  Agent subprocess env contains:
        AGT_TASK_TOKEN  = <signed>
        AGT_SERVER_URL  = http://127.0.0.1:8420
        AGT_TASK_ID     = <task_id>
        AGT_DAEMON_PORT = 8421
        AGT_DAEMON_TOKEN = <random>

  And ``TaskToken.verify(AGT_TASK_TOKEN, AGT_SERVER_SECRET)`` round-trips.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import pytest

from agt.daemon.config import DaemonConfig
from agt.server.services.auth_service import TaskToken
from agt.task.executor import TaskExecutor
from agt.task.models import Task


SECRET = "e2e-test-secret-do-not-use-in-prod"


def _make_env() -> dict[str, str]:
    """Build the same env Daemon would, but using TaskExecutor._build_env directly."""
    cfg = DaemonConfig(
        server_secret=SECRET,
        daemon_id="e2e-daemon",
        daemon_port=8421,
        local_token="e2e-local-token",
    )
    task = Task(
        prompt="noop",
        provider="claude",
        task_id="e2e-task-1",
        work_dir=Path("/tmp/agt/e2e-task-1"),
    )
    executor = TaskExecutor(runtime=None, config=cfg)  # type: ignore[arg-type]
    return executor._build_env(task)


def test_subprocess_env_has_task_token() -> None:
    """_build_env injects AGT_TASK_TOKEN, AGT_SERVER_URL, AGT_TASK_ID."""
    env = _make_env()
    assert "AGT_TASK_TOKEN" in env
    assert env["AGT_SERVER_URL"] == "http://127.0.0.1:8420"
    assert env["AGT_TASK_ID"] == "e2e-task-1"
    assert env["AGT_DAEMON_PORT"] == "8421"
    assert env["AGT_DAEMON_TOKEN"] == "e2e-local-token"


def test_task_token_round_trip() -> None:
    """Injected AGT_TASK_TOKEN verifies under the shared secret."""
    env = _make_env()
    claims = TaskToken.verify(env["AGT_TASK_TOKEN"], SECRET)
    assert claims["task_id"] == "e2e-task-1"
    assert claims["sub"] == "agent:e2e-daemon"
    assert claims["workspace_id"] == "/tmp/agt/e2e-task-1"


def test_task_token_rejects_wrong_secret() -> None:
    """Wrong secret must fail to verify (defense against token tampering)."""
    env = _make_env()
    with pytest.raises(PermissionError):
        TaskToken.verify(env["AGT_TASK_TOKEN"], "wrong-secret")


def test_no_token_when_secret_unset() -> None:
    """Without AGT_SERVER_SECRET, no token injection (silent skip)."""
    cfg = DaemonConfig(
        server_secret="",
        daemon_id="e2e-daemon",
        daemon_port=8421,
        local_token="e2e-local-token",
    )
    task = Task(
        prompt="noop",
        provider="claude",
        task_id="e2e-task-2",
        work_dir=Path("/tmp/agt/e2e-task-2"),
    )
    executor = TaskExecutor(runtime=None, config=cfg)  # type: ignore[arg-type]
    env = executor._build_env(task)
    assert "AGT_TASK_TOKEN" not in env
    assert "AGT_SERVER_URL" not in env
    assert "AGT_TASK_ID" not in env


def test_daemon_secret_file_injects_verifiable_task_token(
    tmp_path, monkeypatch,
) -> None:
    """Daemon's shared-file secret fallback enables MCP/callback tokens."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.delenv("AGT_SERVER_SECRET", raising=False)
    secret_path = fake_home / ".agt" / "server.secret"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text(SECRET, encoding="utf-8")

    cfg = DaemonConfig.from_env()
    task = Task(
        prompt="noop",
        provider="claude",
        task_id="e2e-task-file-secret",
        work_dir=tmp_path / "workspace",
    )
    env = TaskExecutor(  # type: ignore[arg-type]
        runtime=None, config=cfg,
    )._build_env(task)

    claims = TaskToken.verify(env["AGT_TASK_TOKEN"], SECRET)
    assert claims["task_id"] == "e2e-task-file-secret"


def test_daemon_secret_env_overrides_file(tmp_path, monkeypatch) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGT_SERVER_SECRET", "env-secret")
    secret_path = fake_home / ".agt" / "server.secret"
    secret_path.parent.mkdir(parents=True)
    secret_path.write_text("file-secret", encoding="utf-8")

    cfg = DaemonConfig.from_env()

    assert cfg.server_secret == "env-secret"


def test_daemon_session_not_leaked() -> None:
    """Daemon's own runtime/connection info must NOT appear in subprocess env."""
    env = _make_env()
    # Things we explicitly want absent:
    assert "AGT_SERVER_URL_SECRET" not in env
    # Daemon-internal signals we don't want leaking:
    for forbidden in (
        "AGT_DAEMON_REGISTRATION",
        "AGT_DAEMON_SESSION_ID",
        "AGT_DB_PATH",
    ):
        assert forbidden not in env


def test_real_subprocess_inherits_token() -> None:
    """Spawn a python subprocess and confirm it can read AGT_TASK_TOKEN and verify it."""
    env = _make_env()
    # Use a python one-liner that prints claims read from the inherited env.
    script = (
        "import os, json; "
        "from agt.server.services.auth_service import TaskToken; "
        "tok = os.environ['AGT_TASK_TOKEN']; "
        "claims = TaskToken.verify(tok, '" + SECRET + "'); "
        "print(json.dumps({'task_id': claims['task_id'], 'sub': claims['sub']}))"
    )

    async def _spawn() -> tuple[str, str, int]:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-c",
            script,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        return stdout.decode(), stderr.decode(), proc.returncode or 0

    stdout, stderr, rc = asyncio.run(_spawn())
    assert rc == 0, f"subprocess failed: {stderr}"
    import json as _json

    payload = _json.loads(stdout)
    assert payload["task_id"] == "e2e-task-1"
    assert payload["sub"] == "agent:e2e-daemon"
