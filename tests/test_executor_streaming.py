"""Integration tests for ``TaskExecutor._execute_streaming``.

Phase 5: spawn a fake agent subprocess that emits Claude Code's
``--output-format stream-json`` events, and assert the executor's
``TaskResult`` has the right shape — ``stdout`` is plain text (not
JSON Lines), ``session_id`` is taken from the structured ``result``
event, and resume rejection is detected via the detector (not stderr
text).

Each test runs the coroutine through ``asyncio.run`` instead of
``@pytest.mark.asyncio`` so this test module has no plugin
dependency.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agt.daemon.config import DaemonConfig
from agt.runtime.models import AgentEntry, Runtime
from agt.task.executor import TaskExecutor
from agt.task.models import Task

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeProcess:
    """Stand-in for ``asyncio.subprocess.Process`` that pipes fixture data."""

    def __init__(self, stdout_text: str = "", stderr_text: str = "", returncode: int = 0):
        self.stdout_text = stdout_text
        self.stderr_text = stderr_text
        self.returncode = returncode

    def attach(self):
        """Build StreamReader objects inside a running event loop.

        Splitting this from __init__ avoids the Python 3.12 deprecation
        around StreamReader() / feed_data() needing an event loop.
        """
        from asyncio import StreamReader

        self.stdout = StreamReader()
        self.stderr = StreamReader()
        self.stdout.feed_data(self.stdout_text.encode("utf-8"))
        self.stdout.feed_eof()
        self.stderr.feed_data(self.stderr_text.encode("utf-8"))
        self.stderr.feed_eof()

    async def wait(self):
        return self.returncode

    def kill(self):
        pass


class _HangingProcess:
    """Process whose streams never close — simulates a hung agent."""

    def attach(self):
        from asyncio import StreamReader

        self.stdout = StreamReader()
        self.stderr = StreamReader()

    async def wait(self):
        await asyncio.sleep(10)

    def kill(self):
        pass


def _runtime_with_provider(provider: str) -> Runtime:
    entry = AgentEntry(
        command="claude",
        provider=provider,
        display_name="Test",
        path=Path("/bin/claude"),
        version="test",
        cli_style="claude",
    )
    return Runtime(
        name="test-runtime",
        type=provider,
        version="test",
        status="online",
        agent_entry=entry,
        device_name="test-device",
    )


def _patched_subprocess(fake_factory):
    """Return a context manager that mocks both binary exists + subprocess spawn."""
    from contextlib import ExitStack
    from unittest.mock import patch

    stack = ExitStack()
    stack.enter_context(patch("pathlib.Path.exists", lambda self: True))
    stack.enter_context(patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=fake_factory)))
    return stack


def _config(**overrides) -> DaemonConfig:
    return DaemonConfig(**overrides)


def _load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_streaming_path_returns_plain_text_stdout(tmp_path):
    """``TaskResult.stdout`` MUST equal concatenated text deltas — no JSON."""
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    async def go():
        fake = _FakeProcess(stdout_text=_load_fixture("claude_stream_json_sample.jsonl"))
        fake.attach()
        with _patched_subprocess(lambda *a, **k: fake):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())

    assert result.status == "completed"
    # stdout is plain text — no JSON markers.
    assert '{"type"' not in result.stdout
    assert '"session_id"' not in result.stdout
    # session_id came from the structured result event, not from text regex.
    assert result.session_id is not None
    # The sample contains an assistant text block "Hello there friend".
    assert "Hello" in result.stdout


def test_streaming_path_detects_resume_rejection(tmp_path):
    """Resume failure → ``resume_rejected=True``, ``session_id=None``."""
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
        resume_session_id="bad-sid",
    )

    async def go():
        fake = _FakeProcess(stdout_text=_load_fixture("claude_stream_json_resume_error.jsonl"))
        fake.attach()
        with _patched_subprocess(lambda *a, **k: fake):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())

    assert result.resume_rejected is True
    # Cleared so next claim starts fresh (not pointing at the bad sid).
    assert result.session_id is None


def test_streaming_path_timeout_returns_status_timeout(tmp_path):
    """Streaming path honours ``agent_timeout`` just like the pipe path."""
    executor = TaskExecutor(
        runtime=_runtime_with_provider("claude"), config=_config(agent_timeout=1)
    )
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    async def go():
        hanging = _HangingProcess()
        hanging.attach()
        with _patched_subprocess(lambda *a, **k: hanging):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())

    assert result.status == "timeout"


def test_pipe_path_unchanged_for_non_claude_providers(tmp_path):
    """Providers without streaming (``NoopSink``) use the legacy pipe-text path.

    The executor should not add stream-json / --verbose args for them.
    """
    executor = TaskExecutor(runtime=_runtime_with_provider("cursor"), config=_config())
    task = Task(
        prompt="hi",
        provider="cursor",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    captured_cmd: list[list[str]] = []

    async def fake_exec(*args, **kwargs):
        captured_cmd.append(list(args))
        return _FakeProcess(stdout_text="session id: abc-123\n", stderr_text="", returncode=0)

    async def go():
        def make_fake(*a, **k):
            fake = _FakeProcess(stdout_text="session id: abc-123\n", returncode=0)
            fake.attach()
            captured_cmd.append(list(a))
            return fake

        with _patched_subprocess(make_fake):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())

    assert captured_cmd, "subprocess was never spawned"
    argv = captured_cmd[0]
    assert "--output-format" not in argv
    assert "stream-json" not in argv
    # session_id was extracted from the captured stdout text via the legacy
    # text-regex path (still working).
    assert result.session_id == "abc-123"


def test_pipe_path_prints_stdout_live(tmp_path, capsys):
    """The pipe-text path must stream stdout to the terminal as it arrives.

    Regression guard: the Phase 5 refactor replaced the pre-existing
    ``StreamReader`` (which prints each line via its default
    ``on_stdout``) with a silent ``_read_lines`` collector that appended
    to a list without printing — so codex / cursor / copilot etc. chats
    returned with zero visible output. ``result.stdout`` must also keep
    its newlines (contract A: plain text).
    """
    executor = TaskExecutor(
        runtime=_runtime_with_provider("codex"), config=_config(),
    )
    task = Task(
        prompt="hi",
        provider="codex",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    async def go():
        def make_fake(*a, **k):
            fake = _FakeProcess(stdout_text="hello\nworld\n", returncode=0)
            fake.attach()
            return fake

        with _patched_subprocess(make_fake):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())

    captured = capsys.readouterr()
    # stdout was printed live to the terminal.
    assert "hello" in captured.out
    assert "world" in captured.out
    # and the captured plain-text stdout kept its newlines.
    assert result.stdout == "hello\nworld\n"


def test_streaming_args_injected_for_claude(tmp_path):
    """For Claude the executor MUST pass ``--output-format stream-json --verbose``."""
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    captured_cmd: list[list[str]] = []

    async def go():
        def make_fake(*a, **k):
            fake = _FakeProcess(
                stdout_text='{"type":"result","is_error":false,"session_id":"x"}',
                returncode=0,
            )
            fake.attach()
            captured_cmd.append(list(a))
            return fake

        with _patched_subprocess(make_fake):
            return await executor.execute(task, interactive=False)

    asyncio.run(go())

    argv = captured_cmd[0]
    assert "--output-format" in argv
    idx = argv.index("--output-format")
    assert argv[idx + 1] == "stream-json"
    assert "--verbose" in argv


# ── Phase 5: remote-streaming progress sampler ──────────────────────


class _SlowFeedProcess:
    """Fake subprocess that feeds stdout chunks over time.

    Gives the progress sampler time to wake up between text additions.
    """

    def __init__(self, chunks: list[str], returncode: int = 0) -> None:
        self.chunks = chunks
        self.returncode = returncode
        self._feed_task: asyncio.Task | None = None

    def attach(self) -> None:
        from asyncio import StreamReader

        self.stdout = StreamReader()
        self.stderr = StreamReader()
        self.stderr.feed_eof()
        self._feed_task = asyncio.create_task(self._feed())

    async def _feed(self) -> None:
        for chunk in self.chunks:
            await asyncio.sleep(0.05)
            self.stdout.feed_data(chunk.encode("utf-8"))
        self.stdout.feed_eof()

    async def wait(self) -> int:
        assert self._feed_task is not None
        await self._feed_task
        return self.returncode

    def kill(self) -> None:
        pass


def test_progress_callback_receives_growing_prefix(tmp_path, monkeypatch):
    """When ``progress_cb`` is supplied, the sampler calls it with the
    cumulative stdout prefix whenever the prefix grows."""
    monkeypatch.setattr(
        "agt.task.executor._PROGRESS_FLUSH_INTERVAL", 0.02, raising=True
    )
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )
    progress_calls: list[str] = []

    async def progress_cb(prefix: str) -> None:
        progress_calls.append(prefix)

    async def go():
        chunks = [
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"Hello"}}}\n',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":" "}}}\n',
            '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"world"}}}\n',
            '{"type":"result","is_error":false,"session_id":"x"}\n',
        ]
        fake = _SlowFeedProcess(chunks=chunks)
        fake.attach()
        with _patched_subprocess(lambda *a, **k: fake):
            return await executor.execute(
                task, interactive=False, progress_cb=progress_cb
            )

    result = asyncio.run(go())
    assert result.status == "completed"
    assert result.stdout == "Hello world"
    # We should see at least one progress report, and each report must be a
    # prefix of the final accumulated text (cumulative idempotency).
    assert progress_calls
    assert all(
        result.stdout.startswith(p) and len(p) <= len(result.stdout)
        for p in progress_calls
    )
    # Monotonic growth: each subsequent call is at least as long as the last.
    assert all(
        len(progress_calls[i]) <= len(progress_calls[i + 1])
        for i in range(len(progress_calls) - 1)
    )


def test_progress_callback_exception_is_swallowed(tmp_path):
    """A raising ``progress_cb`` must not abort the run or break result capture."""
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    async def progress_cb(_prefix: str) -> None:
        raise RuntimeError("network blip")

    async def go():
        fake = _FakeProcess(
            stdout_text='{"type":"result","is_error":false,"session_id":"x"}',
            returncode=0,
        )
        fake.attach()
        with _patched_subprocess(lambda *a, **k: fake):
            return await executor.execute(
                task, interactive=False, progress_cb=progress_cb
            )

    result = asyncio.run(go())
    assert result.status == "completed"
    assert result.session_id == "x"


def test_no_callback_runs_without_sampler(tmp_path):
    """Omitting ``progress_cb`` keeps the legacy streaming behavior intact."""
    executor = TaskExecutor(runtime=_runtime_with_provider("claude"), config=_config())
    task = Task(
        prompt="hi",
        provider="claude",
        task_id="t1",
        work_dir=tmp_path,
        agent_cwd=tmp_path,
    )

    async def go():
        fake = _FakeProcess(
            stdout_text='{"type":"result","is_error":false,"session_id":"x"}',
            returncode=0,
        )
        fake.attach()
        with _patched_subprocess(lambda *a, **k: fake):
            return await executor.execute(task, interactive=False)

    result = asyncio.run(go())
    assert result.status == "completed"
    assert result.session_id == "x"