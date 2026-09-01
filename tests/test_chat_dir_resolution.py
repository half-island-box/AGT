"""agt chat --dir resolution: local-daemon auto-detect vs. explicit --dir.

Behavior contract (cwd-injection supplement, Phase 4 workdir):
- ``--dir <abs path>`` is forwarded as ``agent_cwd`` through submit_task.
- With ``--daemon`` but no ``--dir``, the CLI checks whether a daemon
  process is alive on this machine (``~/.agt/daemon.pid``, 链A) AND its
  id matches ``--daemon`` (derived from ``~/.agt/daemons`` local entry,
  链B). A mismatch (or no local daemon) errors out with exit 2.
- Without ``--daemon`` (local-controller path) the cwd is unchanged.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli
from agt.daemon.config import AGT_DAEMON_NAMESPACE


# ── Helpers ────────────────────────────────────────────────────────


def _stub_server(calls):
    class _Stub:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def create_job(self, **kw):
            calls["create_job"] = kw
            return {"job_id": "j1", "task_id": "t1", "status": "queued"}
        async def job_input(self, job_id, prompt):
            return {"job_id": job_id, "task_id": "t2", "status": "queued"}
        async def stream_task(self, task_id):
            yield {"type": "done", "status": "completed", "stdout": "",
                   "stderr": "", "session_id": None, "exit_code": 0}
        async def wait_task(self, tid, **kw):
            return {"task_id": tid, "provider": kw.get("provider", "claude"),
                    "status": "completed", "stdout": "", "stderr": "",
                    "exit_code": 0, "duration": 0.0, "session_id": None}
    return _Stub


LOCAL_URL = "ws://0.0.0.0:8422"
CLOUD_URL = "ws://203.0.113.10:8422"


def _derived_id(url: str) -> str:
    return str(uuid.uuid5(AGT_DAEMON_NAMESPACE, url))


def _seed(
    monkeypatch,
    tmp_path,
    *,
    daemons_lines: list[str] | None,
    pid_alive: bool,
) -> Path:
    """Point HOME at a fake home; optionally write daemons + pid files.

    ``daemons_lines=None`` → no ~/.agt/daemons file at all. The pid file
    gets this test process's pid (alive) or an unlikely dead pid.
    """
    home = tmp_path / "home"
    (home / ".agt").mkdir(parents=True, exist_ok=True)
    if daemons_lines is not None:
        (home / ".agt" / "daemons").write_text(
            "\n".join(daemons_lines) + "\n", encoding="utf-8"
        )
    (home / ".agt" / "daemon.pid").write_text(
        f"{os.getpid()}\n" if pid_alive else "999999999\n", encoding="utf-8"
    )
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    return home


def _invoke(*args):
    # One prompt line so a turn actually runs (submit_task fires), then
    # EOF cleanly exits the loop.
    return CliRunner().invoke(cli, list(args), input="hi\n")


# ── agent_cwd wiring through _remote_chat_step_id ───────────────────
#
# The full chat loop is hard to drive from a unit test (interactivity +
# stdin EOF). We test the Job/Step creation dispatch layer
# (``_remote_chat_step_id``) directly so the agent_cwd forwarding is
# observable.


class TestChatTurnRemoteAgentCwd:
    def _stub(self, captured: dict):
        class _Stub:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def create_job(self, **kw):
                captured["kw"] = kw
                return {"job_id": "j1", "task_id": "t1", "status": "queued"}
            async def job_input(self, job_id, prompt):
                return {"job_id": job_id, "task_id": "t2", "status": "queued"}
        return _Stub

    def test_agent_cwd_forwarded_to_create_job(self, tmp_path) -> None:
        from agt.cli import main as cli_mod

        captured: dict = {}
        target = tmp_path / "explicit-dir"
        target.mkdir()

        with patch("agt.cli.main.ServerClient", self._stub(captured)):
            cli_mod._remote_chat_step_id(
                "http://x",
                job_id=None, prompt="hi", provider="claude",
                target_daemon_id="d1", work_dir_str=None,
                identity_did="did:agt:reviewer",
                agent_cwd=target,
            )
        assert captured["kw"]["agent_cwd"] == str(target)

    def test_agent_cwd_none_when_not_provided(self) -> None:
        from agt.cli import main as cli_mod

        captured: dict = {}
        with patch("agt.cli.main.ServerClient", self._stub(captured)):
            cli_mod._remote_chat_step_id(
                "http://x",
                job_id=None, prompt="hi", provider="claude",
                target_daemon_id="d1", work_dir_str=None,
                identity_did=None,
                agent_cwd=None,
            )
        assert captured["kw"].get("agent_cwd") is None


# ── chat() local-daemon resolution (链A pid + 链B daemons-file id) ──


class TestChatCmdResolution:
    def _run_chat(self, args, monkeypatch, tmp_path, *, stub=True):
        calls: dict = {}
        ctx = patch("agt.cli.main.ServerClient", _stub_server(calls)) if stub else patch.dict("sys.modules", {})
        with ctx, patch("agt.cli.main._pick_provider", return_value="claude"), \
             patch("agt.cli.main._pid_is_agt_daemon", return_value=True):
            result = _invoke(*args)
        return result, calls

    def test_local_daemon_match_uses_user_cwd(
        self, tmp_path, monkeypatch
    ) -> None:
        """The user's actual bug: cloud entry + local entry both on 8422;
        a running local daemon whose derived id matches --daemon → the
        user's cwd is forwarded as agent_cwd, no --dir required."""
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[
                f"daemon-cloud-01 {CLOUD_URL}",
                f"local {LOCAL_URL}",
            ],
            pid_alive=True,
        )
        monkeypatch.chdir(tmp_path)
        result, calls = self._run_chat(
            ["chat", "--daemon", _derived_id(LOCAL_URL),
             "--provider", "claude"],
            monkeypatch, tmp_path,
        )
        assert result.exit_code in (0, 1), result.output
        assert calls["create_job"]["agent_cwd"] == str(tmp_path.resolve())

    def test_cloud_only_file_errors_even_when_port_matches(
        self, tmp_path, monkeypatch
    ) -> None:
        """Old foot-gun: a daemons file listing only the cloud daemon must
        NOT make --daemon=<cloud id> pass as local, even with the daemon
        process alive (it would be adopting the cloud identity)."""
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[f"daemon-cloud-01 {CLOUD_URL}"],
            pid_alive=True,
        )
        monkeypatch.chdir(tmp_path)
        result, _ = self._run_chat(
            ["chat", "--daemon", _derived_id(CLOUD_URL)], monkeypatch, tmp_path,
        )
        assert result.exit_code == 2
        assert "not this machine's local daemon" in result.output
        # local id is None (no self entry) — the message says so.
        assert "no local entry" in result.output

    def test_local_daemon_pid_dead_errors(self, tmp_path, monkeypatch) -> None:
        """链A gate: daemons file has a local entry but the pid is dead →
        treated as offline, --dir required."""
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[f"local {LOCAL_URL}"],
            pid_alive=False,
        )
        monkeypatch.chdir(tmp_path)
        result, _ = self._run_chat(
            ["chat", "--daemon", _derived_id(LOCAL_URL)], monkeypatch, tmp_path,
        )
        assert result.exit_code == 2
        assert "offline" in result.output

    def test_local_daemon_id_mismatch_errors(self, tmp_path, monkeypatch) -> None:
        """Daemon alive with a different id than --daemon → exit 2, and
        the error names the running local id."""
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[f"local {LOCAL_URL}"],
            pid_alive=True,
        )
        monkeypatch.chdir(tmp_path)
        result, _ = self._run_chat(
            ["chat", "--daemon", "some-other-id"], monkeypatch, tmp_path,
        )
        assert result.exit_code == 2
        assert f"id={_derived_id(LOCAL_URL)}" in result.output

    def test_no_pid_file_errors(self, tmp_path, monkeypatch) -> None:
        home = tmp_path / "home"
        (home / ".agt").mkdir(parents=True)
        (home / ".agt" / "daemons").write_text(
            f"local {LOCAL_URL}\n", encoding="utf-8"
        )
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.chdir(tmp_path)
        result, _ = self._run_chat(
            ["chat", "--daemon", _derived_id(LOCAL_URL)], monkeypatch, tmp_path,
        )
        assert result.exit_code == 2
        assert "offline" in result.output

    def test_env_override_changes_local_id(self, tmp_path, monkeypatch) -> None:
        """AGT_DAEMON_ID wins over the daemons-file derivation on the CLI
        side, mirroring DaemonConfig.from_env on the daemon side."""
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[f"local {LOCAL_URL}"],
            pid_alive=True,
        )
        monkeypatch.setenv("AGT_DAEMON_ID", "env-pinned-id")
        monkeypatch.chdir(tmp_path)
        result, calls = self._run_chat(
            ["chat", "--daemon", "env-pinned-id", "--provider", "claude"],
            monkeypatch, tmp_path,
        )
        assert result.exit_code in (0, 1), result.output
        assert calls["create_job"]["agent_cwd"] == str(tmp_path.resolve())

    def test_explicit_dir_skips_local_detection(
        self, tmp_path, monkeypatch
    ) -> None:
        """--dir wins even when no local daemon exists at all."""
        _seed(monkeypatch, tmp_path, daemons_lines=None, pid_alive=False)
        target = tmp_path / "explicit"
        target.mkdir()
        monkeypatch.chdir(tmp_path)
        result, calls = self._run_chat(
            ["chat", "--daemon", "anything", "--provider", "claude",
             "--dir", str(target)],
            monkeypatch, tmp_path,
        )
        assert result.exit_code in (0, 1), result.output
        assert calls["create_job"]["agent_cwd"] == str(target.resolve())

    def test_dir_nonexistent_forwarded_verbatim(
        self, tmp_path, monkeypatch
    ) -> None:
        """``--dir`` names a path on the REMOTE daemon, so it must NOT be
        existence-checked locally. A path absent from this machine is
        forwarded verbatim as agent_cwd; the daemon creates it if missing."""
        _seed(monkeypatch, tmp_path, daemons_lines=None, pid_alive=False)
        target = tmp_path / "no-such-dir"  # deliberately NOT created
        monkeypatch.chdir(tmp_path)
        result, calls = self._run_chat(
            ["chat", "--daemon", "anything", "--provider", "claude",
             "--dir", str(target)],
            monkeypatch, tmp_path,
        )
        assert result.exit_code in (0, 1), result.output
        assert calls["create_job"]["agent_cwd"] == str(target)

    def test_dir_file_path_not_rejected(self, tmp_path, monkeypatch) -> None:
        """No local file/dir check either — the path is interpreted on the
        daemon. A path that happens to be a file locally is still
        forwarded (the daemon validates/creates it on its side)."""
        f = tmp_path / "afile"
        f.write_text("x")
        _seed(monkeypatch, tmp_path, daemons_lines=None, pid_alive=False)
        monkeypatch.chdir(tmp_path)
        result, calls = self._run_chat(
            ["chat", "--daemon", "anything", "--provider", "claude",
             "--dir", str(f)],
            monkeypatch, tmp_path,
        )
        assert result.exit_code in (0, 1), result.output
        assert calls["create_job"]["agent_cwd"] == str(f)

    def test_no_dir_no_daemon_flag_ok(self, tmp_path, monkeypatch) -> None:
        """No --daemon and no --dir → local-controller path; no exit error."""
        _seed(monkeypatch, tmp_path, daemons_lines=None, pid_alive=False)
        monkeypatch.chdir(tmp_path)
        result, _ = self._run_chat(
            ["chat", "--provider", "claude"], monkeypatch, tmp_path,
        )
        assert result.exit_code != 2


# ── helper unit tests ───────────────────────────────────────────────


class TestLocalDaemonAlive:
    def test_missing_pid_file_returns_none(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_alive
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        assert _local_daemon_alive() is None

    def test_garbage_pid_returns_none(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_alive
        home = tmp_path / "home"
        (home / ".agt").mkdir(parents=True)
        (home / ".agt" / "daemon.pid").write_text("not-a-pid\n")
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        assert _local_daemon_alive() is None

    def test_dead_pid_returns_none(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_alive
        home = tmp_path / "home"
        (home / ".agt").mkdir(parents=True)
        (home / ".agt" / "daemon.pid").write_text("999999999\n")
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        assert _local_daemon_alive() is None

    def test_live_pid_returned_when_cmdline_matches(
        self, tmp_path, monkeypatch
    ) -> None:
        from agt.cli import main as cli_mod
        home = tmp_path / "home"
        (home / ".agt").mkdir(parents=True)
        (home / ".agt" / "daemon.pid").write_text(f"{os.getpid()}\n")
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setattr(cli_mod, "_pid_is_agt_daemon", lambda _pid: True)
        assert cli_mod._local_daemon_alive() == os.getpid()

    def test_live_pid_rejected_when_cmdline_is_foreign(
        self, tmp_path, monkeypatch
    ) -> None:
        """Pid reuse guard: a live pid whose cmdline isn't an agt daemon
        must not be reported as the local daemon."""
        from agt.cli import main as cli_mod
        home = tmp_path / "home"
        (home / ".agt").mkdir(parents=True)
        (home / ".agt" / "daemon.pid").write_text(f"{os.getpid()}\n")
        monkeypatch.setattr("pathlib.Path.home", lambda: home)
        monkeypatch.setattr(cli_mod, "_pid_is_agt_daemon", lambda _pid: False)
        assert cli_mod._local_daemon_alive() is None


class TestLocalDaemonId:
    def test_env_override_wins(self, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_id
        monkeypatch.setenv("AGT_DAEMON_ID", "pinned")
        assert _local_daemon_id() == "pinned"

    def test_derives_from_local_entry(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_id
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[f"daemon-cloud-01 {CLOUD_URL}", f"local {LOCAL_URL}"],
            pid_alive=False,  # id derivation doesn't depend on liveness
        )
        assert _local_daemon_id() == _derived_id(LOCAL_URL)

    def test_no_file_returns_none(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_id
        _seed(monkeypatch, tmp_path, daemons_lines=None, pid_alive=False)
        monkeypatch.delenv("AGT_DAEMON_ID", raising=False)
        assert _local_daemon_id() is None

    def test_ws_port_env_respected(self, tmp_path, monkeypatch) -> None:
        from agt.cli.main import _local_daemon_id
        _seed(
            monkeypatch, tmp_path,
            daemons_lines=[
                f"daemon-cloud-01 {CLOUD_URL}",
                "local-alt ws://127.0.0.1:9000",
            ],
            pid_alive=False,
        )
        monkeypatch.delenv("AGT_DAEMON_ID", raising=False)
        monkeypatch.setenv("AGT_DAEMON_WS_PORT", "9000")
        assert _local_daemon_id() == _derived_id("ws://127.0.0.1:9000")
