"""StreamingSink — consume one provider's stream event stream.

Phase 5: local streaming terminal output. Each provider that supports
streaming registers a `StreamingSink` implementation; providers that do not
register a `NoopSink` placeholder. `TaskExecutor` consumes JSON Lines from
the agent subprocess via the sink and prints text deltas in real time.

This module defines the protocol only — `ClaudeStreamSink` is the Phase A
implementation. Codex (`Phase B`) will add its own sink in a follow-up change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SinkResult:
    """Terminal result of a stream sink consumption.

    The executor wraps this into ``TaskResult``. ``text`` is the
    concatenated plain text used as ``TaskResult.stdout`` (preserves the
    "stdout is plain text" contract that downstream consumers rely on).
    """

    session_id: str | None
    is_error: bool
    resume_rejected: bool
    text: str


class StreamingSink(ABC):
    """Consume one provider's stream event stream line by line.

    Implementations MUST be tolerant of unknown event ``type`` values
    (silent skip, no raise). Text deltas reach the terminal via
    :meth:`on_text_delta`, which by default appends to
    :attr:`accumulated_text` and prints with ``flush=True``.

    We intentionally do NOT decorate this with ``@dataclass`` — mixing
    :class:`abc.ABC` with the dataclass metaclass breaks ``accumulated_text``
    initialisation on subclasses. A plain ``__init__`` keeps both halves
    working.
    """

    def __init__(self) -> None:
        self.accumulated_text: list[str] = []

    @abstractmethod
    def consume(self, raw: dict[str, Any]) -> None:
        """Process one parsed JSON event from the agent subprocess."""

    def on_text_delta(self, text: str) -> None:
        """Default text-delta handler: append + flush to stdout.

        ``flush=True`` is required so the user sees incremental output
        instead of line-buffered chunks.
        """
        self.accumulated_text.append(text)
        print(text, end="", flush=True)

    @abstractmethod
    def finalize(self) -> SinkResult: ...


class NoopSink(StreamingSink):
    """Placeholder for providers that do not support streaming.

    The executor routes the request through the legacy pipe-text path when
    this sink is selected; :meth:`consume` is never called.
    """

    def consume(self, raw: dict[str, Any]) -> None:  # pragma: no cover
        return

    def finalize(self) -> SinkResult:
        return SinkResult(session_id=None, is_error=False, resume_rejected=False, text="")


class ClaudeStreamSink(StreamingSink):
    """Consume Claude Code ``--output-format stream-json`` events.

    Observed event shapes (Claude Code 2.1.241 + Claude Haiku 4.5):

    - ``{"type":"system","subtype":"init","session_id":"..."}`` — session start
    - ``{"type":"system","subtype":"thinking_tokens",...}`` — internal; ignored
    - ``{"type":"assistant","message":{"content":[{"type":"text","text":"..."}]}}``
      — full assistant message; the text block(s) carry the user-visible output
    - ``{"type":"result","is_error":bool,"session_id":"...","subtype":"success"|"error_during_execution","errors":[...]}``
      — terminal event with ``session_id`` and final status

    Note: ``stream-json`` on ``-p`` mode also requires ``--verbose`` —
    :meth:`agt.task.executor._stream_args_for` adds it when streaming is
    enabled.

    Resume rejection signals (from real ``--resume <bad>`` output):
    - ``is_error=true``
    - ``subtype="error_during_execution"``
    - ``errors`` array contains a string with "resume" or "session"

    ``resume_rejected`` is left False here; the dedicated
    :class:`ClaudeResumeRejectionDetector` makes the final call so the
    detector and sink stay decoupled.

    With ``--include-partial-messages`` the CLI also emits wrapped
    streaming events::

        {"type":"stream_event","event":{"type":"content_block_delta", "delta":{"type":"text_delta","text":"..."}}}

    We unwrap ``raw["event"]`` and dispatch on the inner type. Once
    any ``text_delta`` is observed, the later ``assistant`` event with
    the full message is suppressed so we do not double-print.
    """

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None
        self._is_error: bool = False
        self._subtype: str | None = None
        self._errors: list[str] = []
        # Becomes True on the first text_delta; from then on we skip
        # the trailing ``assistant`` full-message event so we never
        # print the same text twice.
        self._saw_text_delta: bool = False

    def consume(self, raw: dict[str, Any]) -> None:
        """Dispatch on event ``type``; unknown types are silently skipped.

        Handles three Claude Code CLI shapes:

        1. ``{"type":"assistant",...}`` — full message (legacy / no flag)
        2. ``{"type":"stream_event","event":{...}}`` — Anthropic streaming
           event (``--include-partial-messages``)
        3. ``{"type":"result",...}`` — terminal with session_id + status
        """
        mtype = raw.get("type")
        if mtype == "assistant":
            # Dedup: once deltas have been printing, skip the full
            # message that the CLI emits at the end of the turn.
            if not self._saw_text_delta:
                self._consume_assistant(raw.get("message") or {})
        elif mtype == "stream_event":
            self._consume_stream_event(raw.get("event") or {})
        elif mtype == "result":
            self._consume_result(raw)

    def _consume_stream_event(self, event: dict[str, Any]) -> None:
        """Dispatch a single ``stream_event`` payload.

        Only ``text_delta`` is forwarded to ``on_text_delta`` —
        ``thinking_delta`` (model reasoning) and ``signature_delta``
        (cryptographic signature) are intentionally ignored so the
        terminal shows only user-visible prose.
        """
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype != "content_block_delta":
            # message_start / content_block_start / content_block_stop /
            # message_delta / message_stop are all lifecycle events
            # we currently have no use for — silently ignored.
            return
        delta = event.get("delta") or {}
        if not isinstance(delta, dict):
            return
        dtype = delta.get("type")
        if dtype != "text_delta":
            return
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return
        self._saw_text_delta = True
        self.on_text_delta(text)

    def _consume_assistant(self, message: dict[str, Any]) -> None:
        """Emit ``on_text_delta`` for every ``text`` block in the assistant message.

        Non-text blocks (``thinking``, ``tool_use``, image, etc.) are
        intentionally ignored — the streaming terminal UX should show
        only user-visible assistant prose.
        """
        content = message.get("content") or []
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text")
            if isinstance(text, str) and text:
                self.on_text_delta(text)

    def _consume_result(self, raw: dict[str, Any]) -> None:
        """Capture terminal fields for :meth:`finalize` and the resume detector."""
        self._session_id = raw.get("session_id")
        self._is_error = bool(raw.get("is_error"))
        self._subtype = raw.get("subtype")
        errs = raw.get("errors") or []
        if isinstance(errs, list):
            self._errors = [str(e) for e in errs if e]

    def finalize(self) -> SinkResult:
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,  # detector decides
            text="".join(self.accumulated_text),
        )

    @property
    def subtype(self) -> str | None:
        return self._subtype

    @property
    def errors(self) -> list[str]:
        return list(self._errors)


__all__ = [
    "SinkResult",
    "StreamingSink",
    "NoopSink",
    "ClaudeStreamSink",
]