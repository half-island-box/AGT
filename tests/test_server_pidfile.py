"""Server pid-file lifecycle + helpers (Phase 4, server --detach).

Verifies:
- ``create_app`` lifespan writes ``cfg.pid_file`` during startup and
  removes it during shutdown.
- ``_read_pidfile`` returns None when missing, malformed, stale (pid
  points at a process that no longer exists).
- ``_read_pidfile`` returns the pid when the live process is ours.
"""

from __future__ import annotations

import asyncio
import os

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.main import PID_FILE, _read_pidfile, _signal_pid, create_app


SECRET = "server-pidfile-test-secret"


def _build_app(tmp_path) -> TestClient:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "server.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
        pid_file=str(tmp_path / "server.pid"),
    )
    return TestClient(create_app(cfg))


class TestLifespanWritesPidFile:
    def test_pid_file_written_on_startup(self, tmp_path) -> None:
        client = _build_app(tmp_path)
        with client:
            assert (tmp_path / "server.pid").exists()
            assert (tmp_path / "server.pid").read_text().strip() == str(os.getpid())

    def test_pid_file_removed_on_shutdown(self, tmp_path) -> None:
        client = _build_app(tmp_path)
        with client:
            assert (tmp_path / "server.pid").exists()
        # lifespan exit block ran on TestClient context exit.
        assert not (tmp_path / "server.pid").exists()

    def test_no_pid_file_when_unset(self, tmp_path) -> None:
        """Default cfg has pid_file=None — no file written, no removal needed."""
        cfg = ServerConfig(
            host="127.0.0.1", port=8420,
            db_path=str(tmp_path / "x.db"), secret=SECRET,
            sessions_json_path=str(tmp_path / "no.json"),
        )
        client = TestClient(create_app(cfg))
        with client:
            # No file named server.pid in tmp_path root
            assert not (tmp_path / "server.pid").exists()


class TestReadPidfile:
    def test_missing_returns_none(self, tmp_path) -> None:
        assert _read_pidfile(tmp_path / "no-such.pid") is None

    def test_malformed_returns_none(self, tmp_path) -> None:
        bad = tmp_path / "bad.pid"
        bad.write_text("not-a-number\n")
        assert _read_pidfile(bad) is None

    def test_empty_returns_none(self, tmp_path) -> None:
        empty = tmp_path / "empty.pid"
        empty.write_text("")
        assert _read_pidfile(empty) is None

    def test_live_pid_returned(self, tmp_path) -> None:
        live = tmp_path / "live.pid"
        # ``os.getpid()`` is this test process — definitely alive.
        live.write_text(f"{os.getpid()}\n")
        assert _read_pidfile(live) == os.getpid()

    def test_stale_pid_returns_none(self, tmp_path) -> None:
        stale = tmp_path / "stale.pid"
        # A pid that almost certainly doesn't exist. Use a very large
        # value to avoid colliding with anything.
        stale.write_text("999999999\n")
        assert _read_pidfile(stale) is None

    def test_default_path_resolves_home(self, monkeypatch, tmp_path) -> None:
        """``_read_pidfile(None)`` must compute ``Path.home()/.agt/server.pid``
        at call time so ``HOME`` overrides work in tests."""
        # We don't trust that ``Path.home()`` actually returns ``tmp_path``
        # here (it does on most systems but is system-dependent). Pin it
        # via HOME and create a fake file there.
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".agt").mkdir()
        live = tmp_path / ".agt" / "server.pid"
        live.write_text(f"{os.getpid()}\n")

        result = _read_pidfile(None)  # path=None → default
        assert result == os.getpid()


class TestSignalPid:
    def test_signal_raises_on_dead_pid(self) -> None:
        import pytest

        with pytest.raises(ProcessLookupError):
            _signal_pid(999999999)


def test_pid_file_constant_default_path():
    """The legacy ``PID_FILE`` constant still points at the canonical location."""
    assert str(PID_FILE).endswith(".agt/server.pid")