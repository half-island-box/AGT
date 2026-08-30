"""Phase 4 workdir supplement: daemon enforces ``agent_cwd`` allowlist.

The wire-supplied ``agent_cwd`` (from a claim payload) is accepted only
when its resolved path is the same as, or a descendant of, one of the
roots in ``DaemonConfig.agent_cwd_allowlist``. Default root is
``Path.home()``; override via ``AGT_AGENT_CWD_ALLOWLIST`` (colon-separated
absolute paths; ``/`` = anywhere).

This is a defense-in-depth check — the CLI already validates existence
and ``click.Path(exists=True)``, but the daemon must NOT trust any path
that comes off the wire.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agt.daemon.config import (
    DaemonConfig,
    _is_path_under,
    _parse_allowlist,
)
from agt.daemon.daemon import Daemon


def _daemon_with_allowlist(roots: list[Path]) -> Daemon:
    cfg = DaemonConfig(
        server_base_url="http://127.0.0.1:8420",
        server_secret="test-secret",
        daemon_id="d-allowlist",
        daemon_port=0,
        local_token="tok",
        agent_cwd_allowlist=roots,
    )
    daemon = Daemon(config=cfg)
    # Skip the probe / runtime setup that ``Daemon.start`` performs; the
    # allowlist check fires before any of that.
    daemon._started = True
    daemon._runtimes = {"claude": MagicMock()}
    return daemon


class TestAllowlistParser:
    def test_unset_yields_home(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _parse_allowlist(None) == [tmp_path]

    def test_empty_yields_home(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        assert _parse_allowlist("") == [tmp_path]

    def test_colon_separated(self) -> None:
        roots = _parse_allowlist("/a:/b:/c")
        assert roots == [Path("/a"), Path("/b"), Path("/c")]

    def test_slash_means_anywhere(self) -> None:
        roots = _parse_allowlist("/")
        assert roots == [Path("/")]


class TestIsPathUnder:
    def test_same_path(self, tmp_path) -> None:
        assert _is_path_under(tmp_path, tmp_path) is True

    def test_descendant(self, tmp_path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        deep = sub / "deep"
        deep.mkdir()
        assert _is_path_under(deep, tmp_path) is True

    def test_unrelated_sibling(self, tmp_path) -> None:
        other = tmp_path.parent / "definitely-not-under"
        try:
            assert _is_path_under(other, tmp_path) is False
        finally:
            # tmp_path.parent might be /tmp; nothing to clean up
            pass

    def test_parent_is_not_under_child(self, tmp_path) -> None:
        sub = tmp_path / "sub"
        sub.mkdir()
        assert _is_path_under(tmp_path, sub) is False


class TestDaemonHonorsAgentCwd:
    """``Daemon.run`` should use the wire-supplied agent_cwd when allowed."""

    def test_agent_cwd_under_default_home_passes(
        self, tmp_path, monkeypatch
    ) -> None:
        # Default allowlist is [Path.home()]; chdir HOME so a tmp_path
        # descendant qualifies as "under home".
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        proj = tmp_path / "proj"
        proj.mkdir()

        async def fake_execute(self, task, *, interactive=False, progress_cb=None):
            from dataclasses import dataclass
            @dataclass
            class R:
                status: str = "completed"
                session_id: str | None = None
                resume_rejected: bool = False
                exit_code: int = 0
                stdout: str = ""
                stderr: str = ""
                duration: float = 0.0
            return R()

        import agt.task.executor as exec_mod
        monkeypatch.setattr(exec_mod.TaskExecutor, "execute", fake_execute)
        # Default cfg is constructed from_env; rebuild with explicit allowlist.
        monkeypatch.setattr(
            "agt.daemon.config.DaemonConfig.from_env",
            lambda: DaemonConfig(
                agent_cwd_allowlist=[tmp_path],
            ),
        )
        daemon = Daemon(config=DaemonConfig(agent_cwd_allowlist=[tmp_path]))
        daemon._started = True
        daemon._runtimes = {"claude": MagicMock()}

        import asyncio

        async def _run() -> None:
            await daemon.run(
                provider="claude", prompt="hi",
                task_id="t1", agent_cwd=proj,
            )
        asyncio.run(_run())

    def test_agent_cwd_outside_allowlist_raises(self, tmp_path) -> None:
        """agent_cwd under /etc when allowlist is /home → ValueError."""
        daemon = _daemon_with_allowlist([Path("/home")])

        import asyncio

        async def _run() -> None:
            await daemon.run(
                provider="claude", prompt="hi",
                task_id="t1", agent_cwd=Path("/etc"),
            )

        with pytest.raises(ValueError) as excinfo:
            asyncio.run(_run())
        assert "outside the daemon's allowlist" in str(excinfo.value)

    def test_agent_cwd_root_slash_allows_anything(self, tmp_path) -> None:
        daemon = _daemon_with_allowlist([Path("/")])

        async def fake_execute(self, task, *, interactive=False, progress_cb=None):
            from dataclasses import dataclass
            @dataclass
            class R:
                status: str = "completed"
                session_id: str | None = None
                resume_rejected: bool = False
                exit_code: int = 0
                stdout: str = ""
                stderr: str = ""
                duration: float = 0.0
            return R()

        import agt.task.executor as exec_mod
        import asyncio

        async def _run() -> None:
            await daemon.run(
                provider="claude", prompt="hi",
                task_id="t1", agent_cwd=Path("/etc/some/deep/path"),
            )
        # Patch execute to avoid any actual work.
        orig = exec_mod.TaskExecutor.execute
        try:
            exec_mod.TaskExecutor.execute = fake_execute  # type: ignore[assignment]
            asyncio.run(_run())
        finally:
            exec_mod.TaskExecutor.execute = orig  # type: ignore[assignment]


class TestAllowlistFromEnv:
    def test_AGT_AGENT_CWD_ALLOWLIST_parsed(self, monkeypatch, tmp_path) -> None:
        """DaemonConfig.from_env picks up ``AGT_AGENT_CWD_ALLOWLIST``."""
        monkeypatch.setenv("AGT_AGENT_CWD_ALLOWLIST", "/srv/proj:/var/lib/agt")
        cfg = DaemonConfig.from_env()
        assert cfg.agent_cwd_allowlist == [Path("/srv/proj"), Path("/var/lib/agt")]

    def test_unset_yields_home(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("AGT_AGENT_CWD_ALLOWLIST", "")
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
        cfg = DaemonConfig.from_env()
        assert cfg.agent_cwd_allowlist == [tmp_path]