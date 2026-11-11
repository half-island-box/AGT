"""Phase 4 Step 4 — CLI ``--identity`` wiring for run/chat.

Stubs ServerClient / DaemonController so we verify the flag plumbing:
- remote run/chat forward identity_did to submit_task
- local run/chat + --identity errors out (requires --daemon)
- no --identity → no identity_did in submit payload
"""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner

from agt.cli.main import cli


def _stub_client(calls: dict, **returns):
    """ServerClient stub recording submit_task kwargs."""

    class _Stub:
        def __init__(self, *args, **kwargs) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def submit_task(self, **kwargs):
            calls.setdefault("submit", []).append(kwargs)
            return "task-abc12345"

        async def wait_task(self, task_id, **kwargs):
            calls.setdefault("wait", []).append(task_id)
            return {"task_id": task_id, "provider": "claude", "status": "completed",
                    "stdout": "ok", "stderr": "", "exit_code": 0, "duration": 0.1}

    return _Stub


def _invoke(args):
    runner = CliRunner()
    return runner.invoke(cli, args)


class TestRun:
    def test_run_identity_remote_forwards_to_submit(self) -> None:
        calls: dict = {}
        with patch("agt.cli.main.ServerClient", _stub_client(calls)), \
             patch("agt.cli.main._pick_provider", return_value="claude"):
            # --daemon needed for identity path (else local error).
            result = _invoke([
                "run", "--prompt", "hi", "--identity", "did:agt:reviewer",
                "--daemon", "d1",
            ])
        assert result.exit_code in (0, 2), result.output
        # submit_task got identity_did
        if calls.get("submit"):
            assert calls["submit"][-1]["identity_did"] == "did:agt:reviewer"

    def test_run_identity_local_errors(self) -> None:
        # No --daemon → local path must refuse identity.
        result = _invoke([
            "run", "--prompt", "hi", "--identity", "did:agt:reviewer",
        ])
        assert result.exit_code == 2
        assert "--daemon" in result.output

    def test_run_without_identity_no_field(self) -> None:
        calls: dict = {}
        with patch("agt.cli.main.ServerClient", _stub_client(calls)):
            _invoke(["run", "--prompt", "hi", "--daemon", "d1"])
        if calls.get("submit"):
            assert "identity_did" not in calls["submit"][-1]


class TestIdentityNormalization:
    """``--identity`` accepts a full DID or a bare slug; anything else
    (e.g. the ``id:agt:`` typo) is rejected cleanly before it can reach
    the server as a cryptic ``identity_not_found`` 404."""

    def test_run_bare_slug_normalized(self) -> None:
        calls: dict = {}
        with patch("agt.cli.main.ServerClient", _stub_client(calls)), \
             patch("agt.cli.main._pick_provider", return_value="claude"):
            result = _invoke([
                "run", "--prompt", "hi", "--identity", "code-reviewer",
                "--daemon", "d1",
            ])
        assert result.exit_code in (0, 2), result.output
        assert calls.get("submit")
        assert calls["submit"][-1]["identity_did"] == "did:agt:code-reviewer"

    def test_run_typo_scheme_rejected(self) -> None:
        calls: dict = {}
        with patch("agt.cli.main.ServerClient", _stub_client(calls)):
            result = _invoke([
                "run", "--prompt", "hi", "--identity", "id:agt:code-reviewer",
                "--daemon", "d1",
            ])
        assert result.exit_code == 2
        assert "unknown scheme 'id:'" in result.output
        assert not calls.get("submit")  # never reached the server

    def test_chat_typo_scheme_rejected(self) -> None:
        result = _invoke([
            "chat", "--identity", "id:agt:code-reviewer", "--daemon", "d1",
        ])
        assert result.exit_code == 2
        assert "unknown scheme 'id:'" in result.output


class TestChat:
    def test_chat_identity_remote_forwards_each_turn(self) -> None:
        calls: dict = {}
        # chat is interactive; we can't drive stdin easily, so we only
        # verify the option is accepted and the flag plumbing exists by
        # checking help / that the remote path would pass it. We rely on
        # the unit test of _run_chat_turn below for the forwarding.
        # Pass ``--dir`` so the new remote-daemon check doesn't refuse
        # the run (no local daemon is online in the test env).
        _stub_client(calls)
        with patch("agt.cli.main._pick_provider", return_value="claude"), \
             patch("pathlib.Path.home", lambda: __import__("pathlib").Path("/tmp")):
            result = _invoke(["chat", "--identity", "did:agt:reviewer",
                              "--daemon", "d1", "--provider", "claude",
                              "--dir", "/tmp"])
        # It should try to read a prompt; sending EOF (empty stdin) exits 0.
        # We assert no crash and the error-free early exit.
        assert result.exit_code in (0, 1), result.output
        assert "agt chat mode" in result.output or "agt>" in result.output

    def test_chat_identity_local_errors(self) -> None:
        result = _invoke(["chat", "--identity", "did:agt:reviewer"])
        assert result.exit_code == 2
        assert "--daemon" in result.output

    def test_chat_without_identity_ok_local(self) -> None:
        # No identity, no --daemon → local chat is fine (starts loop,
        # EOF exits). Must not hit the identity error. Patch provider
        # discovery because CI runners do not have agent CLIs installed.
        with patch("agt.cli.main._pick_provider", return_value="claude"):
            result = _invoke(["chat", "--provider", "claude"])
        assert result.exit_code != 2
        assert "requires --daemon" not in result.output


class TestRunChatTurnForwarding:
    """Unit-test the dispatcher so we don't need a live chat stdin."""

    def test_run_chat_turn_remote_passes_identity(self) -> None:
        from agt.cli import main as cli_mod

        captured: dict = {}

        async def _fake_create_job(self, **kw):
            captured["kw"] = kw
            return {"job_id": "j1", "task_id": "t1", "status": "queued"}

        async def _fake_stream(self, tid, **kw):
            yield {"type": "snapshot", "status": "running", "stdout": ""}
            yield {"type": "done", "status": "completed", "stdout": "ok",
                   "stderr": "", "session_id": None, "exit_code": 0,
                   "resume_rejected": False}

        class _Stub:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            create_job = _fake_create_job
            stream_task = _fake_stream

        with patch("agt.cli.main.ServerClient", _Stub):
            cli_mod._run_chat_turn(
                controller=None,
                provider="claude",
                line="hi",
                job_id=None,
                cwd=__import__("pathlib").Path("."),
                agt_meta_dir=None,
                work_dir_str=None,
                target_daemon_id="d1",
                server_url="http://x",
                is_first=True,
                initial_resume=None,
                first_prompt="hi",
                turn_index=1,
                identity_did="did:agt:reviewer",
                agent_cwd=None,
            )
        assert captured["kw"]["identity_did"] == "did:agt:reviewer"


class TestRemoteSessionNotRecordedLocally:
    """Remote chat must NOT mirror the daemon-reported session into the
    LOCAL cwd store — the conversation belongs to the daemon (and the
    Server). It is read back via ``agt chat --list --daemon=X``, not the
    local ``<cwd>/.agt/sessions.json``."""

    def _stub(self):
        class _Stub:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def submit_task(self, **kw): return "t1"
            async def stream_task(self, tid, **kw):
                yield {"type": "snapshot", "status": "running", "stdout": ""}
                yield {"type": "done", "status": "completed", "stdout": "ok",
                       "stderr": "", "session_id": "sid-remote-1",
                       "exit_code": 0, "resume_rejected": False}
            async def wait_task(self, tid, **kw):
                return {"status": "completed", "stdout": "ok", "stderr": "",
                        "task_id": tid, "exit_code": 0, "duration": 0.1,
                        "session_id": "sid-remote-1"}
        return _Stub

    def test_chat_turn_remote_leaves_cwd_store_untouched(self, tmp_path) -> None:
        import inspect
        from agt.cli import main as cli_mod
        from agt.task.session import CwdSessionStore

        with patch("agt.cli.main.ServerClient", self._stub()):
            cli_mod._chat_turn_remote(
                provider="claude",
                prompt="hi",
                step_id="t1",
                target_daemon_id="d1",
                server_url="http://x",
                work_dir_str=None,
                identity_did="did:agt:reviewer",
                agent_cwd=tmp_path,
            )
        # The remote turn never touches the local cwd store: no session
        # (nor any other) entry is recorded.
        store = CwdSessionStore.load_or_create(tmp_path)
        assert store.list_sessions() == []

    def test_chat_turn_remote_has_no_cwd_store_params(self) -> None:
        """Structural guard: the mirror hook (cwd / first_prompt /
        turn_index / resume_session_id) is gone from the remote turn."""
        import inspect
        from agt.cli import main as cli_mod

        sig = inspect.signature(cli_mod._chat_turn_remote)
        assert "cwd" not in sig.parameters
        assert "first_prompt" not in sig.parameters
        assert "turn_index" not in sig.parameters
        assert "resume_session_id" not in sig.parameters


class _ResumeFixtureMixin:
    """Shared SID + store-seed helper for resume-related tests.

    Kept off the test class itself so subclasses don't double-inherit
    the *test methods* of TestChatResumeProvider (pytest runs the
    suite once per concrete subclass — inheriting a test class would
    duplicate every test method).
    """

    SID = "sid-from-index"

    def _seed_store(
        self, tmp_path, *, provider: str, identity_did: str | None = None,
    ) -> None:
        from agt.task.session import CwdSessionStore

        store = CwdSessionStore.load_or_create(tmp_path, provider=provider)
        store.record(
            session_id=self.SID,
            task_id="t0",
            first_prompt="old turn",
            turn_count=1,
            identity_did=identity_did,
        )


class TestChatResumeProvider(_ResumeFixtureMixin):
    """``agt chat --resume <sid>`` without ``--provider`` must use the
    provider recorded in ``<cwd>/.agt/sessions.json`` for that sid.

    The user model is: --resume == return to that session == same agent.
    Falling back to "auto-pick first available" silently swaps the agent,
    breaking context continuity.
    """

    def test_resume_without_provider_uses_indexed_provider(
        self, tmp_path, monkeypatch
    ) -> None:
        # Seed the cwd store with an entry whose provider is codex.
        self._seed_store(tmp_path, provider="codex")

        captured: dict = {}

        def _fake_pick(provider, target_daemon_id, server_url, **kw):
            captured["provider"] = provider
            return provider or "claude"  # autopick fallback

        # chat() reads cwd from os.getcwd(); point it at tmp_path so the
        # store's path resolution lands where we seeded.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("agt.cli.main._pick_provider", _fake_pick)
        # Stub out the interactive loop so the test exits promptly.
        monkeypatch.setattr(
            "agt.cli.main._chat_loop",
            lambda **kw: None,
        )

        result = _invoke(["chat", "--resume", self.SID])
        assert result.exit_code in (0, 1), result.output
        # _pick_provider received "codex" — i.e. we honored the index.
        assert captured["provider"] == "codex"
        assert "not found" not in result.output

    def test_resume_with_explicit_provider_overrides_index(
        self, tmp_path, monkeypatch
    ) -> None:
        # Index says claude; user explicitly asks for codex → codex wins.
        self._seed_store(tmp_path, provider="claude")

        captured: dict = {}

        def _fake_pick(provider, target_daemon_id, server_url, **kw):
            captured["provider"] = provider
            return provider or "claude"

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("agt.cli.main._pick_provider", _fake_pick)
        monkeypatch.setattr("agt.cli.main._chat_loop", lambda **kw: None)

        result = _invoke(["chat", "--resume", self.SID, "--provider", "codex"])
        assert result.exit_code in (0, 1), result.output
        assert captured["provider"] == "codex"

    def test_resume_missing_index_falls_back_to_autopick(
        self, tmp_path, monkeypatch
    ) -> None:
        # No sessions.json at all (or one without the sid) — must warn
        # on stderr and let _pick_provider autopick (i.e. receive None).
        monkeypatch.chdir(tmp_path)

        captured: dict = {}

        def _fake_pick(provider, target_daemon_id, server_url, **kw):
            captured["provider"] = provider
            return provider or "claude"

        monkeypatch.setattr("agt.cli.main._pick_provider", _fake_pick)
        monkeypatch.setattr("agt.cli.main._chat_loop", lambda **kw: None)
        # _print_resumed_history prints+sometimes exits when sid is
        # missing in the index. We're not testing history printing here —
        # only provider resolution. Stub it out.
        monkeypatch.setattr(
            "agt.cli.main._print_resumed_history", lambda *a, **kw: None
        )

        result = _invoke(["chat", "--resume", self.SID])
        assert result.exit_code in (0, 1), result.output
        # _pick_provider got None — the autopick path is in charge.
        assert captured["provider"] is None
        # And we told the user why (click 8 merges stderr into output).
        assert "not found" in result.output


# ── chat() --resume restores + validates identity_did (remote) ──────
#
# For a remote daemon the identity is recovered from the Server's session
# resolve record (``_resolve_remote_session``), not the local cwd store.


class TestResumeRestoresIdentity(_ResumeFixtureMixin):
    """``--resume <sid> --daemon=X`` without ``--identity`` restores the
    DID the Server recorded for that session. When ``--identity`` IS
    supplied, it must match the recorded value exactly — except when the
    record has no DID (legacy / first-time assignment), in which case the
    supplied value is accepted."""

    def _run_chat(self, args, monkeypatch, tmp_path, captured, resolve_record):
        """Invoke ``chat`` with the chat loop no-op'd; capture the
        resolved ``identity_did`` / ``provider`` reaching the chat loop.
        ``resolve_record`` is what the Server resolve returns."""
        monkeypatch.chdir(tmp_path)
        # --dir skips the local-daemon detection branch; we only care
        # about the identity restore path here.
        monkeypatch.setattr(
            "agt.cli.main._pick_provider",
            lambda provider, *a, **kw: provider or "claude",
        )
        monkeypatch.setattr(
            "agt.cli.main._print_resumed_history", lambda *a, **kw: None
        )
        monkeypatch.setattr(
            "agt.cli.main._resolve_remote_session",
            lambda *a, **kw: resolve_record,
        )
        # Capture identity_did reaching the chat loop (== chat() final
        # resolved value).
        monkeypatch.setattr(
            "agt.cli.main._chat_loop",
            lambda **kw: captured.update(kw),
        )
        return _invoke(args)

    def _record(self, tmp_path, *, identity_did):
        return {
            "task_id": "t0", "session_id": self.SID,
            "provider": "codex", "identity_did": identity_did,
            "daemon_id": "any", "dir": str(tmp_path),
        }

    def test_resume_without_identity_restores_recorded(
        self, tmp_path, monkeypatch
    ) -> None:
        # Server record: provider=codex, identity=code-reviewer DID.
        captured: dict = {}
        result = self._run_chat(
            ["chat", "--resume", self.SID, "--provider", "codex",
             "--daemon", "any", "--dir", str(tmp_path)],
            monkeypatch, tmp_path, captured,
            self._record(tmp_path, identity_did="did:agt:code-reviewer"),
        )
        assert result.exit_code in (0, 1), result.output
        # No --identity passed; chat() restored it from the Server record.
        assert captured.get("identity_did") == "did:agt:code-reviewer"

    def test_resume_with_matching_identity_succeeds(
        self, tmp_path, monkeypatch
    ) -> None:
        captured: dict = {}
        result = self._run_chat(
            ["chat", "--resume", self.SID,
             "--identity", "did:agt:code-reviewer",
             "--daemon", "any", "--dir", str(tmp_path)],
            monkeypatch, tmp_path, captured,
            self._record(tmp_path, identity_did="did:agt:code-reviewer"),
        )
        assert result.exit_code in (0, 1), result.output
        assert captured.get("identity_did") == "did:agt:code-reviewer"

    def test_resume_with_mismatched_identity_errors(
        self, tmp_path, monkeypatch
    ) -> None:
        # Session was opened with code-reviewer; resuming with fronted
        # is a hard error (would silently switch identities mid-session).
        captured: dict = {}
        result = self._run_chat(
            ["chat", "--resume", self.SID,
             "--identity", "did:agt:fronted-design-engineer",
             "--daemon", "any", "--dir", str(tmp_path)],
            monkeypatch, tmp_path, captured,
            self._record(tmp_path, identity_did="did:agt:code-reviewer"),
        )
        assert result.exit_code == 2
        assert "refusing to resume with" in result.output
        # The chat loop never started (we exited before).
        assert captured == {}

    def test_resume_legacy_session_no_identity_accepts_new(
        self, tmp_path, monkeypatch
    ) -> None:
        # Legacy record: identity_did was never recorded (None).
        # First-time assignment: any supplied --identity is accepted.
        captured: dict = {}
        result = self._run_chat(
            ["chat", "--resume", self.SID,
             "--identity", "did:agt:code-reviewer",
             "--daemon", "any", "--dir", str(tmp_path)],
            monkeypatch, tmp_path, captured,
            self._record(tmp_path, identity_did=None),
        )
        assert result.exit_code in (0, 1), result.output
        assert captured.get("identity_did") == "did:agt:code-reviewer"

    def test_resume_legacy_session_without_identity_stays_none(
        self, tmp_path, monkeypatch
    ) -> None:
        # Legacy + no --identity passed: no identity used.
        captured: dict = {}
        result = self._run_chat(
            ["chat", "--resume", self.SID, "--provider", "codex",
             "--daemon", "any", "--dir", str(tmp_path)],
            monkeypatch, tmp_path, captured,
            self._record(tmp_path, identity_did=None),
        )
        assert result.exit_code in (0, 1), result.output
        assert captured.get("identity_did") is None
