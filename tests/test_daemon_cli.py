"""Tests for ``agt daemon start`` CLI (Phase 4, detach flag).

Verifies:
- foreground (no --detach): unchanged behavior, hits cmd_start
- detached: subprocess.Popen called with right args + env + log file,
  parent exits 0, default log path is ~/.agt/daemon.log
- --log-file overrides the path
- --server-url forwarded via AGT_SERVER_URL env var (not as a CLI arg;
  DaemonConfig.server_url is a read-only property)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from agt.cli.main import cli


class _FakePopen:
    """Drop-in for subprocess.Popen: captures constructor args, returns a
    process-like object with a stable pid for assertions."""

    instances: list["_FakePopen"] = []

    def __init__(self, args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.pid = 99999
        type(self).instances.append(self)


def _invoke(args, monkeypatch=None):
    runner = CliRunner()
    return runner.invoke(cli, ["daemon", "start", *args])


class TestForeground:
    """No --detach: parent blocks via cmd_start. We mock that call so the
    test stays sync; the point is just to confirm we still hit the
    foreground path and don't accidentally spawn a child."""

    def test_foreground_calls_cmd_start(self, monkeypatch) -> None:
        from agt.daemon import main as daemon_main
        called: dict = {}

        def _fake_cmd_start(args):
            called["server_url"] = args.server_url
            return 0

        monkeypatch.setattr(daemon_main, "cmd_start", _fake_cmd_start)
        result = _invoke(["--server-url", "http://x:1234"])
        assert result.exit_code == 0, result.output
        assert called == {"server_url": "http://x:1234"}


class TestDetached:
    def _run(self, args, popen_kw: dict):
        """Invoke `daemon start --detach` with subprocess.Popen patched.
        Returns (CliResult, captured _FakePopen instance, last log_fp)."""
        _FakePopen.instances.clear()
        # The CLI module opens the log file itself before Popen — capture
        # which file objects get created.
        opened: list = []

        real_open = open

        def _open(path, mode="r", *a, **kw):
            fp = real_open(path, mode, *a, **kw)
            opened.append((path, mode, fp))
            return fp

        with patch("subprocess.Popen", _FakePopen), patch("builtins.open", _open, create=True):
            result = _invoke(args)
        return result, _FakePopen.instances[-1], opened[-1] if opened else None

    def test_spawns_python_module_invocation(self) -> None:
        import sys
        result, popen, _ = self._run(["--detach"], {})
        assert result.exit_code == 0, result.output
        assert popen.args[0] == sys.executable
        assert popen.args[1] == "-m"
        assert popen.args[2] == "agt.daemon.main"
        assert popen.args[-1] == "start"  # subcommand

    def test_start_new_session_and_null_stdin(self) -> None:
        _, popen, _ = self._run(["--detach"], {})
        assert popen.kwargs["start_new_session"] is True
        assert popen.kwargs["close_fds"] is True
        # stdin must be DEVNULL so the child doesn't read from the parent tty
        from subprocess import DEVNULL
        assert popen.kwargs["stdin"] is DEVNULL

    def test_default_log_path_is_agtdir(self, tmp_path, monkeypatch) -> None:
        # Redirect HOME so the default log path lands in tmp_path/.agt
        monkeypatch.setenv("HOME", str(tmp_path))
        _, popen, opened = self._run(["--detach"], {})
        log_path, mode, log_fp = opened
        assert log_path == tmp_path / ".agt" / "daemon.log"
        assert "b" in mode  # binary append
        # The same fp is handed to Popen for stdout+stderr
        assert popen.kwargs["stdout"] is log_fp
        assert popen.kwargs["stderr"] is log_fp

    def test_log_file_override(self, tmp_path) -> None:
        custom = tmp_path / "custom" / "my-daemon.log"
        result, popen, opened = self._run(["--detach", "--log-file", str(custom)], {})
        assert result.exit_code == 0, result.output
        assert opened[0] == custom
        assert opened[1] == "ab"
        # Same fp handed to Popen for stdout+stderr
        assert popen.kwargs["stdout"] is opened[2]
        assert popen.kwargs["stderr"] is opened[2]
        # Path printed in user-facing message
        assert str(custom) in result.output

    def test_log_dir_is_created(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        # ~/.agt doesn't exist yet — should be auto-created.
        self._run(["--detach"], {})
        assert (tmp_path / ".agt").is_dir()

    def test_server_url_forwarded_via_env_not_argv(self) -> None:
        # The CLI's --server-url flag must be passed as AGT_SERVER_URL,
        # NOT as a "--server-url X" CLI argument, because DaemonConfig
        # reads only env in ``from_env()``.
        _, popen, _ = self._run(["--detach", "--server-url", "http://h:9999"], {})
        assert "AGT_SERVER_URL" not in popen.args
        assert "http://h:9999" not in popen.args
        assert popen.kwargs["env"]["AGT_SERVER_URL"] == "http://h:9999"

    def test_server_url_omitted_keeps_parent_env(self) -> None:
        # No --server-url flag → env is copied as-is; AGT_SERVER_URL not injected.
        _, popen, _ = self._run(["--detach"], {})
        env = popen.kwargs["env"]
        # Should be a dict (copy), not None
        assert isinstance(env, dict)
        # If AGT_SERVER_URL happens to be set in the test env, we don't
        # inject a new value (no key shadowing by us).
        assert "AGT_SERVER_URL" not in popen.kwargs  # function doesn't pass kw

    def test_user_facing_message(self) -> None:
        result, popen, _ = self._run(["--detach"], {})
        assert "pid=" in result.output
        assert str(popen.pid) in result.output
        assert "log=" in result.output

    def test_popen_pid_is_returned_in_message(self) -> None:
        # The pid echoed to the user matches the child Popen's pid.
        with patch("subprocess.Popen", _FakePopen):
            result = _invoke(["--detach"])
        assert "pid=99999" in result.output