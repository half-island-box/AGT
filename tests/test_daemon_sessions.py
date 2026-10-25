"""Daemon-session read model + remote chat resume (Option B).

Covers the two Server endpoints added for the remote-session view:

- ``GET /api/daemons/{id}/sessions`` — cross-directory session list for one
  daemon (``--list --daemon=X``), deduped by ``session_id``, with ``dir``
  falling back ``agent_cwd → work_dir``.
- ``GET /api/sessions/{sid}`` — reverse-lookup used by remote
  ``--resume <sid> --daemon=X`` (returns 404 for unknown sessions).

And the CLI layer: ``_resolve_remote_session`` (resolve + daemon validation),
``_print_remote_session_list`` (renders the DIR column), and the ``chat``
command wiring that reuses the resolved ``task_id`` so the Server reopens
the task and the daemon truly resumes the same session.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from starlette.testclient import TestClient

from agt.cli.main import cli
from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, JobModel, SessionModel, StepModel


SECRET = "daemon-sessions-test-secret"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _build_app(tmp_path: Path):
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "server.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(engine.dispose())

    return client, factory, _dispose


async def _seed_task(
    factory, *, task_id, session_id, daemon_id, provider="claude",
    agent_cwd=None, work_dir=None, identity_did=None, prompt="hi",
) -> None:
    now = _now()
    async with factory() as session:
        session.add(JobModel(
            job_id=task_id, status="done", job_type="task",
            provider=provider, identity_did=identity_did,
            agent_cwd=agent_cwd, work_dir=work_dir,
            created_at=now, updated_at=now,
        ))
        if session_id is not None:
            session.add(SessionModel(
                job_id=task_id, identity_did=identity_did or "", provider=provider,
                session_id=session_id, created_at=now, updated_at=now,
            ))
        session.add(StepModel(
            step_id=task_id, job_id=task_id, provider=provider, prompt=prompt,
            status="completed", daemon_id=daemon_id, session_id=session_id,
            agent_cwd=agent_cwd, work_dir=work_dir, identity_did=identity_did,
            created_at=now, updated_at=now,
        ))
        await session.commit()


# ── Server: GET /api/daemons/{id}/sessions ──────────────────────────


class TestListDaemonSessionsRoute:
    def test_lists_sessions_across_dirs(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
                agent_cwd="/proj/a", prompt="first ask",
            ))
            asyncio.run(_seed_task(
                factory, task_id="t2", session_id="s2", daemon_id="d1",
                agent_cwd="/proj/b", provider="codex", prompt="second ask",
            ))

            resp = client.get("/api/daemons/d1/sessions")
            assert resp.status_code == 200
            sessions = resp.json()["sessions"]
            assert len(sessions) == 2
            by_sid = {s["session_id"]: s for s in sessions}
            assert by_sid["s1"]["dir"] == "/proj/a"
            assert by_sid["s1"]["first_prompt"] == "first ask"
            assert by_sid["s1"]["provider"] == "claude"
            assert by_sid["s2"]["dir"] == "/proj/b"
            assert by_sid["s2"]["provider"] == "codex"
        finally:
            dispose()

    def test_dedupes_by_session_id(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            # Same session_id on two task rows (defensive: 1 task == 1
            # session in practice, but never list it twice).
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="dup", daemon_id="d1",
            ))
            asyncio.run(_seed_task(
                factory, task_id="t2", session_id="dup", daemon_id="d1",
            ))

            resp = client.get("/api/daemons/d1/sessions")
            assert resp.status_code == 200
            sessions = resp.json()["sessions"]
            assert len(sessions) == 1
            assert sessions[0]["session_id"] == "dup"
        finally:
            dispose()

    def test_dir_falls_back_to_work_dir(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
                agent_cwd=None, work_dir="/meta/dir",
            ))
            resp = client.get("/api/daemons/d1/sessions")
            assert resp.status_code == 200
            assert resp.json()["sessions"][0]["dir"] == "/meta/dir"
        finally:
            dispose()

    def test_excludes_tasks_without_session(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
            ))
            asyncio.run(_seed_task(
                factory, task_id="t2", session_id=None, daemon_id="d1",
            ))
            resp = client.get("/api/daemons/d1/sessions")
            assert resp.status_code == 200
            sessions = resp.json()["sessions"]
            assert [s["session_id"] for s in sessions] == ["s1"]
        finally:
            dispose()

    def test_only_matches_claiming_daemon(self, tmp_path) -> None:
        """daemon_id (set at claim) is the filter — not target_daemon_id."""
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
            ))
            asyncio.run(_seed_task(
                factory, task_id="t2", session_id="s2", daemon_id="d2",
            ))
            resp = client.get("/api/daemons/d1/sessions")
            assert resp.status_code == 200
            assert [s["session_id"] for s in resp.json()["sessions"]] == ["s1"]
        finally:
            dispose()


# ── Server: GET /api/sessions/{sid} ─────────────────────────────────


class TestResolveSessionRoute:
    def test_resolves_to_task_provider_identity_dir(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            asyncio.run(_seed_task(
                factory, task_id="t1", session_id="s1", daemon_id="d1",
                agent_cwd="/proj/x", identity_did="did:agt:reviewer",
                provider="claude",
            ))
            resp = client.get("/api/sessions/s1")
            assert resp.status_code == 200
            body = resp.json()
            assert body["task_id"] == "t1"
            assert body["session_id"] == "s1"
            assert body["provider"] == "claude"
            assert body["identity_did"] == "did:agt:reviewer"
            assert body["daemon_id"] == "d1"
            assert body["dir"] == "/proj/x"
        finally:
            dispose()

    def test_unknown_session_404(self, tmp_path) -> None:
        client, factory, dispose = _build_app(tmp_path)
        try:
            resp = client.get("/api/sessions/nope")
            assert resp.status_code == 404
        finally:
            dispose()


# ── CLI: _resolve_remote_session ───────────────────────────────────


def _stub_client(*, resolve=None, list_sessions=None, list_runs=None) -> type:
    """An async ServerClient stub patching over ``agt.cli.main.ServerClient``."""

    class _Stub:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def resolve_session(self, session_id):
            if callable(resolve):
                return resolve(session_id)
            return resolve

        async def list_daemon_sessions(self, daemon_id, limit=200):
            if callable(list_sessions):
                return list_sessions(daemon_id)
            return list_sessions

        async def list_runs(self, **kwargs):
            runs = list_runs if list_runs is not None else []
            if callable(runs):
                runs = runs(**kwargs)
            provider = kwargs.get("provider")
            if provider is not None:
                runs = [r for r in runs if r.get("provider") == provider]
            return {"runs": runs, "total": len(runs)}

    return _Stub


class TestResolveRemoteSession:
    def test_returns_record_on_match(self, monkeypatch) -> None:
        from agt.cli import main as cli_mod
        rec = {"task_id": "t9", "session_id": "s9", "provider": "claude",
               "identity_did": None, "daemon_id": "d1", "dir": "/x"}
        with patch("agt.cli.main.ServerClient",
                   _stub_client(resolve=rec)):
            out = cli_mod._resolve_remote_session("s9", "d1", "http://x")
        assert out == rec

    def test_returns_none_and_errors_when_not_found(self, monkeypatch) -> None:
        from agt.cli import main as cli_mod
        with patch("agt.cli.main.ServerClient", _stub_client(resolve=None)):
            out = cli_mod._resolve_remote_session("s9", "d1", "http://x")
        assert out is None

    def test_rejects_daemon_mismatch(self, monkeypatch) -> None:
        from agt.cli import main as cli_mod
        rec = {"task_id": "t9", "session_id": "s9", "provider": "claude",
               "identity_did": None, "daemon_id": "OTHER", "dir": "/x"}
        with patch("agt.cli.main.ServerClient",
                   _stub_client(resolve=rec)):
            out = cli_mod._resolve_remote_session("s9", "d1", "http://x")
        assert out is None

    def test_returns_none_on_transport_error(self, monkeypatch) -> None:
        from agt.cli import main as cli_mod

        def _boom(_sid):
            raise RuntimeError("boom")

        with patch("agt.cli.main.ServerClient",
                   _stub_client(resolve=_boom)):
            out = cli_mod._resolve_remote_session("s9", "d1", "http://x")
        assert out is None


# ── CLI: _print_remote_session_list ────────────────────────────────


class TestPrintRemoteSessionList:
    def test_renders_dir_column(self, capsys) -> None:
        from agt.cli import main as cli_mod
        sessions = [
            {"session_id": "s1", "provider": "claude", "cwd": "/proj/a",
             "updated_at": "now", "title": "hello"},
        ]
        with patch("agt.cli.main.ServerClient",
                   _stub_client(list_runs=sessions)):
            cli_mod._print_remote_session_list("d1", None, "http://x", list_dir="/proj/a")
        out = capsys.readouterr().out
        assert "DIR" in out
        assert "/proj/a" in out
        assert "s1" in out

    def test_filters_by_provider(self, capsys) -> None:
        from agt.cli import main as cli_mod
        sessions = [
            {"session_id": "s1", "provider": "claude", "cwd": "/a",
             "updated_at": "now", "title": "hi"},
            {"session_id": "s2", "provider": "codex", "cwd": "/b",
             "updated_at": "now", "title": "hi"},
        ]
        with patch("agt.cli.main.ServerClient",
                   _stub_client(list_runs=sessions)):
            cli_mod._print_remote_session_list("d1", "codex", "http://x", list_dir="/")
        out = capsys.readouterr().out
        assert "s2" in out
        assert "s1" not in out


# ── CLI: chat --resume <sid> --daemon=X reuses the resolved task_id ──


class TestChatRemoteResumeWiring:
    def test_resume_reuses_job_id(self, tmp_path, monkeypatch) -> None:
        from agt.cli import main as cli_mod
        monkeypatch.chdir(tmp_path)

        rec = {"job_id": "reused-job-1", "task_id": "reused-t1",
               "session_id": "s9", "provider": "claude", "identity_did": None,
               "daemon_id": "d1", "dir": str(tmp_path)}

        captured = {}

        def _fake_loop(**kwargs):
            captured.update(kwargs)

        with patch("agt.cli.main._resolve_remote_session", return_value=rec), \
             patch("agt.cli.main._pick_provider", return_value="claude"), \
             patch("agt.cli.main._chat_loop", side_effect=_fake_loop):
            result = CliRunner().invoke(
                cli,
                ["chat", "--daemon", "d1", "--resume", "s9",
                 "--provider", "claude", "--no-history"],
                input="hi\n",
            )
        assert result.exit_code == 0, result.output
        # The resolved job_id is reused (not a fresh chat-xxxx id) so the
        # Server appends the next turn to the same Job and keeps the
        # session pointer alive.
        assert captured["job_id"] == "reused-job-1"
        assert captured["agent_cwd"] == Path(str(tmp_path))

    def test_resume_resolves_provider_when_omitted(
        self, tmp_path, monkeypatch,
    ) -> None:
        from agt.cli import main as cli_mod
        monkeypatch.chdir(tmp_path)

        rec = {"job_id": "reused-job-1", "task_id": "reused-t1",
               "session_id": "s9", "provider": "codex",
               "identity_did": "did:agt:r", "daemon_id": "d1",
               "dir": str(tmp_path)}

        captured = {}

        def _fake_loop(**kwargs):
            captured.update(kwargs)

        with patch("agt.cli.main._resolve_remote_session", return_value=rec), \
             patch("agt.cli.main._pick_provider", return_value="codex"), \
             patch("agt.cli.main._chat_loop", side_effect=_fake_loop):
            result = CliRunner().invoke(
                cli,
                ["chat", "--daemon", "d1", "--resume", "s9", "--no-history"],
                input="hi\n",
            )
        assert result.exit_code == 0, result.output
        # Provider + identity + job_id recovered from the Server, not the
        # CLI flags.
        assert captured["provider"] == "codex"
        assert captured["identity_did"] == "did:agt:r"
        assert captured["job_id"] == "reused-job-1"
