"""Phase 5 CLI streaming tests.

Covers the cursor-aware SSE rendering helpers
(:func:`agt.cli.main._stream_and_wait` and
:func:`agt.cli.main._poll_fallback`) without a live server — we stub
``ServerClient.stream_task`` and ``wait_task`` and assert what lands on
``sys.stdout``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import httpx
import pytest

from agt.cli import main as cli_mod
from agt.comm.errors import RPCRejected


def _run(coro):
    return asyncio.run(coro)


class _StreamClient:
    """Configurable stub for ``ServerClient`` that drives ``_stream_and_wait``.

    ``stream_events`` is a list of frames to yield. ``fail_n_streams``
    makes the first N calls to ``stream_task`` raise ``httpx.ConnectError``,
    simulating reconnect attempts.
    """

    def __init__(
        self,
        stream_events: list[dict],
        *,
        fail_n_streams: int = 0,
        wait_result: dict | None = None,
    ):
        self.stream_events = list(stream_events)
        self.fail_n_streams = fail_n_streams
        self.wait_result = wait_result or {
            "status": "completed",
            "stdout": "fallback",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.1,
            "session_id": "sid-fb",
        }
        self.stream_calls = 0
        self.wait_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def stream_task(self, task_id, **kw):
        self.stream_calls += 1
        if self.stream_calls <= self.fail_n_streams:
            raise httpx.ConnectError("boom")
        for ev in self.stream_events:
            yield ev

    async def wait_task(self, task_id, **kw):
        self.wait_calls += 1
        return dict(self.wait_result)


def test_stream_and_wait_renders_snapshot_delta_done(capsys):
    """Happy path: snapshot tail + delta + done tail are printed, with a
    trailing newline when the final text doesn't end with one."""
    client = _StreamClient([
        {"type": "snapshot", "status": "running", "stdout": ""},
        {"type": "delta", "chunk": "Hello"},
        {"type": "done", "status": "completed", "stdout": "Hello world!",
         "stderr": "", "session_id": "sid-1", "exit_code": 0,
         "resume_rejected": False},
    ])

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))

    captured = capsys.readouterr()
    assert captured.out == "Hello world!\n"
    assert result["stdout"] == "Hello world!"
    assert result["session_id"] == "sid-1"
    assert client.stream_calls == 1
    assert client.wait_calls == 0


def test_stream_and_wait_realigns_on_snapshot_mid_stream(capsys):
    """A snapshot that arrives after some output is treated as authoritative:
    we print only the unseen tail and reset ``printed``."""
    client = _StreamClient([
        {"type": "snapshot", "status": "running", "stdout": "already"},
        {"type": "delta", "chunk": " printed"},
        {"type": "done", "status": "completed",
         "stdout": "already printed\n", "stderr": "",
         "session_id": None, "exit_code": 0, "resume_rejected": False},
    ])

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))

    captured = capsys.readouterr()
    # The snapshot already contained the full text, so the delta appended
    # one space; the done tail is "\n", which already ends the line.
    assert captured.out == "already printed\n"
    assert result["stdout"] == "already printed\n"


def test_stream_and_wait_reconnects_once_on_transport_error(capsys):
    """First ``stream_task`` dies with a transport error; the helper retries
    once and succeeds on the second attempt."""
    client = _StreamClient(
        [
            {"type": "snapshot", "status": "running", "stdout": ""},
            {"type": "done", "status": "completed", "stdout": "ok\n",
             "stderr": "", "session_id": "sid-2", "exit_code": 0,
             "resume_rejected": False},
        ],
        fail_n_streams=1,
    )

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))

    captured = capsys.readouterr()
    assert captured.out == "ok\n"
    assert result["session_id"] == "sid-2"
    assert client.stream_calls == 2
    assert client.wait_calls == 0


def test_stream_and_wait_reconnects_then_falls_back_to_wait_task(capsys):
    """Two transport failures (or a dead stream) → use legacy ``wait_task``.
    The fallback prints only ``stdout[printed:]`` to avoid re-printing."""
    client = _StreamClient(
        [],  # stream ends immediately without ``done``
        fail_n_streams=1,
        wait_result={
            "status": "completed",
            "stdout": "some prior text and the rest",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.5,
            "session_id": "sid-fb",
        },
    )

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))

    captured = capsys.readouterr()
    # ``printed`` stayed 0 because the stream never yielded content, so we
    # print the full fallback stdout plus a trailing newline.
    assert captured.out == "some prior text and the rest\n"
    assert result["session_id"] == "sid-fb"
    assert client.stream_calls == 2
    assert client.wait_calls == 1


def test_stream_and_wait_cursor_aware_fallback(capsys):
    """If the stream delivered some content before failing, the fallback
    prints only the unseen tail."""
    client = _StreamClient(
        [
            {"type": "snapshot", "status": "running", "stdout": "hello"},
        ],
        fail_n_streams=0,
        wait_result={
            "status": "completed",
            "stdout": "hello world",
            "stderr": "",
            "exit_code": 0,
            "duration": 0.5,
            "session_id": "sid-fb2",
        },
    )

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))

    captured = capsys.readouterr()
    # Snapshot printed "hello" (printed==0 at subscribe time), then the
    # stream ended without done → fallback prints only the " world" tail.
    assert captured.out == "hello world\n"
    assert result["stdout"] == "hello world"


def test_stream_and_wait_rpc_rejected_triggers_fallback():
    """An RPCRejected from ``stream_task`` (e.g. old server without the
    endpoint) is treated like a transport error: retry once, then fallback."""
    client = _StreamClient([], fail_n_streams=2, wait_result={
        "status": "completed", "stdout": "fallback", "stderr": "",
        "exit_code": 0, "duration": 0.1, "session_id": "sid-rpc",
    })

    result = _run(cli_mod._stream_and_wait(client, "t1", "d1"))
    assert client.stream_calls == 2
    assert client.wait_calls == 1
    assert result["session_id"] == "sid-rpc"


def test_poll_fallback_cursor_aware_and_finishes_line(capsys):
    """``_poll_fallback`` prints only ``stdout[printed:]`` and lands the
    prompt on a fresh line."""
    client = _StreamClient([], wait_result={
        "status": "completed", "stdout": "prefix-tail", "stderr": "",
        "exit_code": 0, "duration": 0.1, "session_id": "sid-poll",
    })

    result = _run(cli_mod._poll_fallback(client, "t1", "d1", printed=len("prefix-")))

    captured = capsys.readouterr()
    assert captured.out == "tail\n"
    assert result["session_id"] == "sid-poll"


def test_poll_fallback_timeout_shows_offline_warning(capsys):
    """``wait_task`` raising ``TimeoutError`` surfaces the existing offline
    warning and returns ``None``."""

    class _TimeoutClient:
        async def wait_task(self, task_id, **kw):
            raise TimeoutError("no response")

    result = _run(cli_mod._poll_fallback(_TimeoutClient(), "t1", "d1", printed=0))

    captured = capsys.readouterr()
    assert result is None
    assert "No response" in captured.err
    assert "d1" in captured.err


def test_stream_and_wait_propagates_keyboard_interrupt():
    """Ctrl-C during streaming propagates out of ``_stream_and_wait`` — it is
    not swallowed by the ``except (RequestError, RPCRejected)`` fallback."""

    class _InterruptClient:
        async def stream_task(self, task_id, **kw):
            # ``yield`` marks this as an async generator so ``async for``
            # iterates; the first ``__anext__`` raises, simulating Ctrl-C.
            if False:
                yield None
            raise KeyboardInterrupt()

        async def wait_task(self, task_id, **kw):
            raise TimeoutError("no")

    with pytest.raises(KeyboardInterrupt):
        _run(cli_mod._stream_and_wait(_InterruptClient(), "t1", "d1"))
