"""Tests for ``agt server start/stop/status`` CLI (Phase 4, server detach).

Verifies the Popen spawn behavior (foreground bypasses Popen; detached
mode passes the right args/env/log file) plus the new stop/status
commands.
"""

from __future__ import annotations

import os
import signal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from click.testing import CliRunner

from agt.cli.main import cli
from agt.server.config import ServerConfig


@pytest.fixture(autouse=True)
def _server_secret_env(monkeypatch, request):
    """Set AGT_SERVER_SECRET for every test except missing-secret cases."""
    if "no_server_secret" not in request.keywords:
        monkeypatch.setenv("AGT_SERVER_SECRET", "x" * 64)


class _FakePopen:
    """Records constructor args; returns a process-like with a stable pid."""

    instances: list = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 77777
        type(self).instances.append(self)


def _invoke(args, popen_cls=_FakePopen):
    runner = CliRunner()
    return runner.invoke(cli, ["server", *args])


# ── foreground (no --detach) ───────────────────────────────────────


class TestForeground:
    def test_foreground_calls_cli_main(self, monkeypatch) -> None:
        from agt.server import main as server_main
        monkeypatch.setattr(server_main, "_read_pidfile", lambda _p: None)
        called: dict = {}

        def _fake_cli_main(args):
            called["host"] = args.host
            called["port"] = args.port
            return 0

        monkeypatch.setattr(server_main, "cli_main", _fake_cli_main)
        result = _invoke(["start", "--host", "0.0.0.0", "--port", "9999"])
        assert result.exit_code == 0, result.output
        assert called == {"host": "0.0.0.0", "port": 9999}

    def test_foreground_does_not_spawn_popen(self, monkeypatch) -> None:
        from agt.server import main as server_main
        monkeypatch.setattr(server_main, "_read_pidfile", lambda _p: None)
        monkeypatch.setattr(server_main, "cli_main", lambda _args: 0)
        with patch("subprocess.Popen", _FakePopen):
            _invoke(["start"])
        assert _FakePopen.instances == []


# ── detached spawn ─────────────────────────────────────────────────


class TestDetachedSpawn:
    def _run(self, args, popen_cls=_FakePopen):
        _FakePopen.instances.clear()
        opened: list = []

        real_open = open

        def _open(path, mode="r", *a, **kw):
            fp = real_open(path, mode, *a, **kw)
            opened.append((path, mode, fp))
            return fp

        with patch("subprocess.Popen", popen_cls), \
             patch("builtins.open", _open, create=True), \
             patch("agt.server.pidfile._read_pidfile", return_value=None):
            result = _invoke(["start", *args])
        return result, _FakePopen.instances[-1], opened[-1] if opened else None

    def test_spawns_module_invocation(self) -> None:
        result, popen, _ = self._run(["-d"], _FakePopen)
        assert result.exit_code == 0, result.output
        assert popen.args[1] == "-m"
        assert popen.args[2] == "agt.server.main"
        assert popen.args[3:] == [
            "--host", "127.0.0.1",
            "--port", "8420",
            "--log-file", os.path.expanduser("~/.agt/server.log"),
            "--pid-file", os.path.expanduser("~/.agt/server.pid"),
            "--detach",
        ]

    def test_popen_detached_session(self) -> None:
        _, popen, _ = self._run(["-d"], _FakePopen)
        assert popen.kwargs["start_new_session"] is True
        assert popen.kwargs["close_fds"] is True
        from subprocess import DEVNULL
        assert popen.kwargs["stdin"] is DEVNULL

    def test_db_path_forwarded_when_set(self) -> None:
        _, popen, _ = self._run(["-d", "--db-path", "/tmp/x.db"], _FakePopen)
        assert "--db-path" in popen.args
        assert popen.args[popen.args.index("--db-path") + 1] == "/tmp/x.db"

    def test_db_path_omitted_when_unset(self) -> None:
        _, popen, _ = self._run(["-d"], _FakePopen)
        assert "--db-path" not in popen.args

    def test_default_log_path(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _, _, opened = self._run(["-d"], _FakePopen)
        assert opened[0] == tmp_path / ".agt" / "server.log"

    def test_log_file_override(self, tmp_path) -> None:
        custom = tmp_path / "logs" / "my-server.log"
        _, popen, opened = self._run(["-d", "--log-file", str(custom)], _FakePopen)
        assert opened[0] == custom
        # Log file path passed to child CLI
        idx = popen.args.index("--log-file")
        assert popen.args[idx + 1] == str(custom)

    def test_default_pid_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _, popen, _ = self._run(["-d"], _FakePopen)
        # Pid file path passed to child CLI
        idx = popen.args.index("--pid-file")
        assert popen.args[idx + 1] == str(tmp_path / ".agt" / "server.pid")
        # Also exposed via env for the stop/status subcommands to find.
        assert popen.kwargs["env"]["AGT_SERVER_PID_FILE"] == str(
            tmp_path / ".agt" / "server.pid"
        )

    def test_pid_file_override(self, tmp_path) -> None:
        custom = tmp_path / "x.pid"
        _, popen, _ = self._run(["-d", "--pid-file", str(custom)], _FakePopen)
        idx = popen.args.index("--pid-file")
        assert popen.args[idx + 1] == str(custom)
        assert popen.kwargs["env"]["AGT_SERVER_PID_FILE"] == str(custom)

    def test_pid_file_env_var_preserved(self) -> None:
        """If AGT_SERVER_PID_FILE is set in parent env, do not clobber it
        when --pid-file is also explicitly given (the explicit flag wins).
        """
        _, popen, _ = self._run(["-d", "--pid-file", "/tmp/explicit.pid"], _FakePopen)
        assert popen.kwargs["env"]["AGT_SERVER_PID_FILE"] == "/tmp/explicit.pid"

    def test_user_facing_message(self) -> None:
        result, popen, _ = self._run(["-d"], _FakePopen)
        assert "pid=" in result.output
        assert str(popen.pid) in result.output
        assert "log=" in result.output


# ── stop-if-running (start --detach replaces a stale process) ───────


class TestStopIfRunning:
    def test_stops_existing_server(self, monkeypatch, tmp_path) -> None:
        from agt.cli import main as cli_mod
        from agt.server import pidfile as server_pidfile

        pid_path = tmp_path / "server.pid"
        pid_path.write_text("424242\n")
        monkeypatch.setattr(server_pidfile, "_read_pidfile", lambda _p: 424242)

        captured: dict = {}

        def _fake_stop(pid, label, timeout=10.0):
            captured["pid"] = pid
            captured["label"] = label

        monkeypatch.setattr(cli_mod, "_stop_if_running", _fake_stop)

        _FakePopen.instances.clear()
        with patch("subprocess.Popen", _FakePopen):
            result = _invoke(["start", "-d", "--pid-file", str(pid_path)])
        assert result.exit_code == 0, result.output
        assert captured == {"pid": 424242, "label": "server"}

    def test_no_stop_when_not_running(self, monkeypatch) -> None:
        from agt.cli import main as cli_mod
        from agt.server import pidfile as server_pidfile

        monkeypatch.setattr(server_pidfile, "_read_pidfile", lambda _p: None)
        captured: dict = {}

        def _fake_stop(pid, label, timeout=10.0):
            captured["pid"] = pid

        monkeypatch.setattr(cli_mod, "_stop_if_running", _fake_stop)

        _FakePopen.instances.clear()
        with patch("subprocess.Popen", _FakePopen):
            result = _invoke(["start", "-d"])
        assert result.exit_code == 0, result.output
        assert captured == {}

    def test_stops_existing_foreground(self, monkeypatch, tmp_path) -> None:
        """Foreground ``server start`` must also replace a running server —
        otherwise it fails to bind the port ("address already in use")."""
        from agt.cli import main as cli_mod
        from agt.server import main as server_main
        from agt.server import pidfile as server_pidfile

        pid_path = tmp_path / "server.pid"
        pid_path.write_text("424242\n")
        monkeypatch.setattr(server_pidfile, "_read_pidfile", lambda _p: 424242)
        # Foreground delegates to cli_main; stub it so uvicorn doesn't run.
        monkeypatch.setattr(server_main, "cli_main", lambda _args: 0)

        captured: dict = {}

        def _fake_stop(pid, label, timeout=10.0):
            captured["pid"] = pid
            captured["label"] = label

        monkeypatch.setattr(cli_mod, "_stop_if_running", _fake_stop)

        result = _invoke(["start", "--pid-file", str(pid_path)])
        assert result.exit_code == 0, result.output
        assert captured == {"pid": 424242, "label": "server"}


# ── stop / status ──────────────────────────────────────────────────


class TestStopStatus:
    def test_status_writes_pid_file(self, tmp_path) -> None:
        """When the lifespan wrote the pid file, ``status`` reports it."""
        import signal

        pid_path = tmp_path / "server.pid"
        pid_path.write_text(f"{os.getpid()}\n")
        result = _invoke(["status", "--pid-file", str(pid_path)])
        assert result.exit_code == 0, result.output
        assert "is running" in result.output
        assert str(os.getpid()) in result.output

    def test_status_missing_pid_file_exits_nonzero(self, tmp_path) -> None:
        result = _invoke(["status", "--pid-file", str(tmp_path / "no.pid")])
        assert result.exit_code == 1
        assert "NOT running" in result.output

    def test_status_uses_env_var(self, tmp_path, monkeypatch) -> None:
        pid_path = tmp_path / "envvar.pid"
        pid_path.write_text(f"{os.getpid()}\n")
        monkeypatch.setenv("AGT_SERVER_PID_FILE", str(pid_path))
        result = _invoke(["status"])
        assert result.exit_code == 0, result.output
        assert str(os.getpid()) in result.output

    def test_stop_sends_sigterm(self, tmp_path, monkeypatch) -> None:
        """Stop reads the pid file and sends SIGTERM to that pid."""
        captured: dict = {}
        pid_path = tmp_path / "server.pid"
        pid_path.write_text(f"{os.getpid()}\n")

        def _fake_signal(pid, sig=signal.SIGTERM):
            captured["pid"] = pid
            captured["sig"] = sig

        # server_stop does `from ..server.pidfile import _signal_pid` inside
        # the function, so patch the module attribute it reads from.
        from agt.server import pidfile as server_pidfile
        monkeypatch.setattr(server_pidfile, "_signal_pid", _fake_signal)

        result = _invoke(["stop", "--pid-file", str(pid_path)])
        assert result.exit_code == 0, result.output
        assert captured["pid"] == os.getpid()
        assert captured["sig"] == signal.SIGTERM
        assert "SIGTERM" in result.output

    def test_stop_missing_pidfile_is_noop(self, tmp_path) -> None:
        result = _invoke(["stop", "--pid-file", str(tmp_path / "no.pid")])
        assert result.exit_code == 0
        assert "No server running" in result.output


# ── server init ────────────────────────────────────────────────────


class TestServerInit:
    def test_init_creates_secret_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        result = _invoke(["init"])
        assert result.exit_code == 0, result.output
        secret_path = tmp_path / ".agt" / "server.secret"
        assert secret_path.exists()
        secret = secret_path.read_text(encoding="utf-8").strip()
        assert len(secret) == 64
        int(secret, 16)  # valid hex
        assert oct(secret_path.stat().st_mode)[-3:] == "600"
        assert "Generated server secret" in result.output

    def test_init_is_idempotent(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        secret_path = tmp_path / ".agt" / "server.secret"
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text("a" * 64, encoding="utf-8")
        result = _invoke(["init"])
        assert result.exit_code == 0, result.output
        assert secret_path.read_text(encoding="utf-8").strip() == "a" * 64
        assert "already exists" in result.output

    def test_init_force_regenerates(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        secret_path = tmp_path / ".agt" / "server.secret"
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text("a" * 64, encoding="utf-8")
        result = _invoke(["init", "--force"])
        assert result.exit_code == 0, result.output
        new_secret = secret_path.read_text(encoding="utf-8").strip()
        assert new_secret != "a" * 64
        assert len(new_secret) == 64


# ── missing secret error handling ──────────────────────────────────


class TestMissingSecret:
    @pytest.mark.no_server_secret
    def test_start_without_secret_shows_friendly_message(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("AGT_SERVER_SECRET", raising=False)
        result = _invoke(["start"])
        assert result.exit_code == 1, result.output
        assert "agt server init" in result.output
        assert "Traceback" not in result.output

    def test_config_reads_secret_file(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("AGT_SERVER_SECRET", raising=False)
        secret_path = tmp_path / ".agt" / "server.secret"
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text("b" * 64, encoding="utf-8")
        cfg = ServerConfig.from_env()
        assert cfg.secret == "b" * 64

    def test_env_var_takes_precedence_over_file(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("AGT_SERVER_SECRET", "c" * 64)
        secret_path = tmp_path / ".agt" / "server.secret"
        secret_path.parent.mkdir(parents=True)
        secret_path.write_text("d" * 64, encoding="utf-8")
        cfg = ServerConfig.from_env()
        assert cfg.secret == "c" * 64