"""StreamingSink — consume one provider's stream event stream.

Phase 5: local streaming terminal output. Each provider that supports
streaming registers a `StreamingSink` implementation; providers that do not
register a `NoopSink` placeholder. `TaskExecutor` consumes JSON Lines from
the agent subprocess via the sink and prints text deltas in real time.

This module defines the protocol only — `ClaudeStreamSink` is the Phase A
implementation. Codex (`Phase B`) will add its own sink in a follow-up change.

Structured agent events (thinking / tool use / tool result / token usage)
ride a second, provider-agnostic channel next to the plain-text stream. Each
event is a flat dict::

    {"seq": 4, "kind": "tool_use_start", "text_offset": 120,
     "block_id": "msg_1:1", "tool_use_id": "call_1", "name": "Bash"}

``seq`` is globally monotonic per Step and ``text_offset`` is the length of
the cumulative plain text at the moment the event was emitted, which lets a
client splice events back into the text stream. Text itself never enters the
event channel — ``TaskResult.stdout`` stays plain text.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# One event's JSON payload is capped so a full-file ``tool_result`` cannot
# bloat the SSE stream or the ``step_events`` table. Overlong strings are
# clipped head+tail with a marker.
MAX_EVENT_BYTES = 32 * 1024

# Per-Step cap on stored events. Once exceeded, the oldest detail delta is
# dropped first; the structural start/end/result/usage nodes are kept so a
# replay still renders a coherent transcript.
MAX_EVENTS_PER_STEP = 5000

_TRUNCATION_MARKER = "\n…[truncated]…\n"

# Event kinds that survive the cap; detail deltas are evicted first.
_KEEP_KINDS = frozenset(
    {
        "thinking_start",
        "thinking_end",
        "tool_use_start",
        "tool_use_end",
        "tool_result",
        "usage",
    }
)

_USAGE_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


def _clip(text: str, limit: int) -> str:
    """Clip ``text`` to roughly ``limit`` chars, keeping head and tail."""
    if limit <= 0 or len(text) <= limit:
        return text
    budget = max(2, limit - len(_TRUNCATION_MARKER))
    head = budget // 2
    tail = budget - head
    return f"{text[:head]}{_TRUNCATION_MARKER}{text[-tail:]}"


def _fit_fields(fields: dict[str, Any], *, limit: int = MAX_EVENT_BYTES) -> dict[str, Any]:
    """Cap one event's payload at ``limit`` serialized bytes.

    Strings are clipped head+tail — a ``tool_result`` carrying a whole file
    is the realistic offender. A structured ``input`` dict that alone blows
    the budget is replaced with a clipped JSON blob so the event still makes
    sense to a reader. ``truncated=true`` marks any lossy event.
    """
    working = dict(fields)
    for _ in range(6):
        encoded = json.dumps(working, ensure_ascii=False)
        over = len(encoded.encode("utf-8")) - limit
        if over <= 0:
            break
        candidates = [k for k, v in working.items() if isinstance(v, str)]
        key = max(candidates, key=lambda k: len(working[k]), default=None)
        if key is None or len(working[key]) <= 64:
            break
        working[key] = _clip(working[key], max(64, len(working[key]) - over))
        working["truncated"] = True

    encoded = json.dumps(working, ensure_ascii=False)
    if len(encoded.encode("utf-8")) > limit and isinstance(working.get("input"), dict):
        raw = json.dumps(working["input"], ensure_ascii=False)
        working["input"] = {"truncated": True, "json": _clip(raw, limit // 2)}
        working["truncated"] = True
    return working


def _usage_fields(usage: dict[str, Any] | None) -> dict[str, int]:
    """Extract the billing-relevant counters present on ``usage``."""
    if not isinstance(usage, dict):
        return {}
    return {
        key: usage[key]
        for key in _USAGE_KEYS
        if isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool)
    }


def _content_text(content: Any) -> str:
    """Render a ``tool_result.content`` value as text.

    Only the string shape has been observed in Claude Code traces; arrays
    (images) are JSON-serialized so nothing is silently dropped.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)


@dataclass(frozen=True)
class SinkResult:
    """Terminal result of a stream sink consumption.

    The executor wraps this into ``TaskResult``. ``text`` is the
    concatenated plain text used as ``TaskResult.stdout`` (preserves the
    "stdout is plain text" contract that downstream consumers rely on).
    ``events`` carries the structured thinking/tool/usage events and
    ``usage`` the turn-level token summary (``result.usage``), when the
    provider emitted them.
    """

    session_id: str | None
    is_error: bool
    resume_rejected: bool
    text: str
    events: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] | None = None


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
        # Length of the cumulative text, maintained incrementally so every
        # event can stamp an exact ``text_offset`` without re-joining.
        self._text_len: int = 0
        # Optional observer for structured events (local CLI rendering).
        # NEVER used for the remote path — that rides progress/report.
        self.on_event: Callable[[dict[str, Any]], None] | None = None

    @abstractmethod
    def consume(self, raw: dict[str, Any]) -> None:
        """Process one parsed JSON event from the agent subprocess."""

    def on_text_delta(self, text: str) -> None:
        """Default text-delta handler: append + flush to stdout.

        ``flush=True`` is required so the user sees incremental output
        instead of line-buffered chunks.
        """
        self.accumulated_text.append(text)
        self._text_len += len(text)
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

    Observed event shapes (Claude Code 2.1.285):

    - ``{"type":"system","subtype":"init","model":"..."}`` — session start
    - ``{"type":"system","subtype":"thinking_tokens",...}`` — internal; ignored
    - ``{"type":"assistant","message":{"id":"msg_...","content":[...]}}``
      — one line per content block; the text block(s) carry user-visible output
    - ``{"type":"user","message":{"content":[{"type":"tool_result",...}]}}``
      — tool results fed back to the model
    - ``{"type":"result","is_error":bool,...}`` — terminal aggregate
      (``usage``, ``total_cost_usd``, ``num_turns``)
    - ``{"type":"stream_event","event":{...}}`` — wrapped Anthropic streaming
      events (``--include-partial-messages``)

    Text deltas keep the original behaviour (printed live, later ``assistant``
    text suppressed). Thinking / tool_use / tool_result / usage additionally
    produce structured events on :attr:`events`.

    Block state is keyed by ``(message_id, index)``: the CLI emits one
    ``assistant`` line per content block and reuses indices across messages,
    so a plain index key would collide. The same key is what suppresses the
    trailing ``assistant`` duplicate of a block already streamed.
    """

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None
        self._is_error: bool = False
        self._subtype: str | None = None
        self._errors: list[str] = []
        # Becomes True on the first text_delta; from then on we skip
        # the trailing ``assistant`` full-message text so we never
        # print the same text twice.
        self._saw_text_delta: bool = False

        # ── Structured event state ────────────────────────────────
        self.events: deque[dict[str, Any]] = deque()
        self._seq: int = 0
        # Turn-level usage summary from the ``result`` aggregate.
        self.usage: dict[str, Any] | None = None
        self._model: str | None = None
        self._current_message_id: str | None = None
        # Input-side usage buffered from ``message_start`` until the
        # matching ``message_delta`` supplies the output side.
        self._message_input_usage: dict[str, dict[str, int]] = {}
        # (message_id, index) -> {"type", "tool_use_id", "name", "json", ...}
        self._blocks: dict[str, dict[str, Any]] = {}

    # ── Public dispatch ─────────────────────────────────────────

    def consume(self, raw: dict[str, Any]) -> None:
        """Dispatch on event ``type``; unknown types are silently skipped."""
        mtype = raw.get("type")
        if mtype == "assistant":
            self._consume_assistant(raw.get("message") or {})
        elif mtype == "user":
            self._consume_user(raw)
        elif mtype == "system":
            self._consume_system(raw)
        elif mtype == "stream_event":
            self._consume_stream_event(raw.get("event") or {})
        elif mtype == "result":
            self._consume_result(raw)

    # ── Event emission ──────────────────────────────────────────

    def _emit(self, kind: str, **fields: Any) -> None:
        """Append one structured event and notify the optional observer."""
        event: dict[str, Any] = {
            "seq": self._seq,
            "kind": kind,
            "text_offset": self._text_len,
        }
        self._seq += 1
        # Budget the detail fields so the *whole* serialized event (envelope
        # included) stays under the cap; the envelope itself is tiny.
        envelope = len(json.dumps(event, ensure_ascii=False).encode("utf-8"))
        event.update(
            _fit_fields(fields, limit=max(1024, MAX_EVENT_BYTES - envelope))
        )
        self.events.append(event)
        self._evict_if_needed()
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # pragma: no cover - observer must not break streams
                log.debug("on_event observer raised", exc_info=True)

    def _evict_if_needed(self) -> None:
        """Enforce :data:`MAX_EVENTS_PER_STEP`, evicting detail deltas first."""
        while len(self.events) > MAX_EVENTS_PER_STEP:
            for index, event in enumerate(self.events):
                if event.get("kind") not in _KEEP_KINDS:
                    del self.events[index]
                    break
            else:
                self.events.popleft()

    # ── Streaming events ────────────────────────────────────────

    def _consume_stream_event(self, event: dict[str, Any]) -> None:
        """Dispatch a single ``stream_event`` payload."""
        if not isinstance(event, dict):
            return
        etype = event.get("type")
        if etype == "message_start":
            self._on_message_start(event)
        elif etype == "content_block_start":
            self._on_block_start(event)
        elif etype == "content_block_delta":
            self._on_block_delta(event)
        elif etype == "content_block_stop":
            self._on_block_stop(event)
        elif etype == "message_delta":
            self._on_message_delta(event)
        # message_stop and anything unknown: no use for them.

    def _on_message_start(self, event: dict[str, Any]) -> None:
        message = event.get("message") or {}
        if not isinstance(message, dict):
            return
        message_id = message.get("id")
        if isinstance(message_id, str) and message_id:
            self._current_message_id = message_id
        model = message.get("model")
        if isinstance(model, str) and model:
            self._model = model
        usage = _usage_fields(message.get("usage"))
        if usage:
            self._message_input_usage[self._message_key()] = usage

    def _on_message_delta(self, event: dict[str, Any]) -> None:
        output = _usage_fields(event.get("usage"))
        if not output:
            return
        merged = {
            **self._message_input_usage.pop(self._message_key(), {}),
            **output,
        }
        merged["scope"] = "message"
        if self._model:
            merged["model"] = self._model
        self._emit("usage", **merged)

    def _on_block_start(self, event: dict[str, Any]) -> None:
        block = event.get("content_block") or {}
        if not isinstance(block, dict):
            return
        key = self._block_key(event.get("index"))
        btype = block.get("type")
        if btype == "thinking":
            self._blocks[key] = {"type": "thinking", "text": [], "done": False}
            self._emit("thinking_start", block_id=key)
        elif btype == "tool_use":
            self._blocks[key] = {
                "type": "tool_use",
                "tool_use_id": block.get("id"),
                "name": block.get("name") or "",
                "json": [],
                "done": False,
            }
            self._emit(
                "tool_use_start",
                block_id=key,
                tool_use_id=block.get("id"),
                name=block.get("name") or "",
            )

    def _on_block_delta(self, event: dict[str, Any]) -> None:
        delta = event.get("delta") or {}
        if not isinstance(delta, dict):
            return
        key = self._block_key(event.get("index"))
        dtype = delta.get("type")
        if dtype == "thinking_delta":
            text = delta.get("thinking")
            if not isinstance(text, str) or not text:
                return
            block = self._blocks.get(key)
            if block is not None:
                block.setdefault("text", []).append(text)
            self._emit("thinking_delta", block_id=key, text=text)
        elif dtype == "input_json_delta":
            partial = delta.get("partial_json")
            block = self._blocks.get(key)
            if isinstance(partial, str) and block is not None:
                block.setdefault("json", []).append(partial)
        elif dtype == "text_delta":
            text = delta.get("text")
            if not isinstance(text, str) or not text:
                return
            self._saw_text_delta = True
            self.on_text_delta(text)

    def _on_block_stop(self, event: dict[str, Any]) -> None:
        key = self._block_key(event.get("index"))
        block = self._blocks.get(key)
        if block is None or block.get("done"):
            return
        block["done"] = True
        if block.get("type") == "thinking":
            self._emit("thinking_end", block_id=key)
        elif block.get("type") == "tool_use":
            self._emit(
                "tool_use_end",
                block_id=key,
                tool_use_id=block.get("tool_use_id"),
                name=block.get("name") or "",
                input=self._parse_block_input(block),
            )

    @staticmethod
    def _parse_block_input(block: dict[str, Any]) -> Any:
        raw = "".join(block.get("json") or [])
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (TypeError, ValueError):
            return {"_raw": raw}

    # ── Full-message fallback (no partial events) ───────────────

    def _consume_assistant(self, message: dict[str, Any]) -> None:
        """Render the trailing ``assistant`` line, deduplicating streamed blocks.

        Text keeps the original ``_saw_text_delta`` dedupe. Thinking/tool
        blocks that were already produced from partial events are skipped by
        the ``(message_id, index)`` key; the fallback path emits the whole
        block at once.
        """
        content = message.get("content") or []
        if not isinstance(content, list):
            return
        message_id = message.get("id") or self._current_message_id or "?"
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                if self._saw_text_delta:
                    continue
                text = block.get("text")
                if isinstance(text, str) and text:
                    self.on_text_delta(text)
            elif btype == "thinking":
                if self._seen_block(message_id, index, "thinking"):
                    continue
                text = block.get("thinking")
                if not isinstance(text, str) or not text:
                    continue
                key = f"{message_id}:{index}"
                self._blocks[key] = {"type": "thinking", "text": [text], "done": True}
                self._emit("thinking_start", block_id=key)
                self._emit("thinking_delta", block_id=key, text=text)
                self._emit("thinking_end", block_id=key)
            elif btype == "tool_use":
                if self._seen_block(message_id, index, "tool_use"):
                    continue
                key = f"{message_id}:{index}"
                tool_use_id = block.get("id")
                name = block.get("name") or ""
                self._blocks[key] = {
                    "type": "tool_use",
                    "tool_use_id": tool_use_id,
                    "name": name,
                    "json": [],
                    "done": True,
                }
                self._emit(
                    "tool_use_start",
                    block_id=key,
                    tool_use_id=tool_use_id,
                    name=name,
                )
                self._emit(
                    "tool_use_end",
                    block_id=key,
                    tool_use_id=tool_use_id,
                    name=name,
                    input=block.get("input") or {},
                )

    def _consume_user(self, raw: dict[str, Any]) -> None:
        """Emit one ``tool_result`` event per result block on a ``user`` line."""
        message = raw.get("message") or {}
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            self._emit(
                "tool_result",
                tool_use_id=block.get("tool_use_id"),
                content=_content_text(block.get("content")),
                is_error=bool(block.get("is_error")),
            )

    def _consume_system(self, raw: dict[str, Any]) -> None:
        """Capture the model name from the ``init`` event."""
        if raw.get("subtype") != "init":
            return
        model = raw.get("model")
        if isinstance(model, str) and model:
            self._model = model

    # ── Terminal aggregate ──────────────────────────────────────

    def _consume_result(self, raw: dict[str, Any]) -> None:
        """Capture terminal fields for :meth:`finalize` and the resume detector."""
        self._session_id = raw.get("session_id")
        self._is_error = bool(raw.get("is_error"))
        self._subtype = raw.get("subtype")
        errs = raw.get("errors") or []
        if isinstance(errs, list):
            self._errors = [str(e) for e in errs if e]

        summary = _usage_fields(raw.get("usage"))
        if not summary:
            return
        summary["scope"] = "turn"
        model = self._model or self._model_from_model_usage(raw)
        if model:
            summary["model"] = model
        cost = raw.get("total_cost_usd")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            summary["cost_usd"] = float(cost)
        turns = raw.get("num_turns")
        if isinstance(turns, int) and not isinstance(turns, bool):
            summary["num_turns"] = turns
        self.usage = summary
        self._emit("usage", **summary)

    @staticmethod
    def _model_from_model_usage(raw: dict[str, Any]) -> str | None:
        model_usage = raw.get("modelUsage")
        if isinstance(model_usage, dict) and model_usage:
            return str(next(iter(model_usage)))
        return None

    # ── Helpers ─────────────────────────────────────────────────

    def _message_key(self) -> str:
        return self._current_message_id or "?"

    def _block_key(self, index: Any) -> str:
        return f"{self._message_key()}:{index}"

    def _seen_block(self, message_id: str, index: int, btype: str) -> bool:
        """True when a block was already emitted from partial events.

        Matches on the exact ``(message_id, index)`` key. When the message
        id was unknown while streaming (no ``message_start`` id), blocks are
        stored under the ``?`` placeholder, so we also match on ``index`` +
        type against those only — never across two real message ids, which
        reuse the same indices and would drop a legitimate block.
        """
        if f"{message_id}:{index}" in self._blocks:
            return True
        if self._current_message_id is not None and message_id == self._current_message_id:
            return False
        suffix = f":{index}"
        return any(
            k.startswith("?:") and k.endswith(suffix) and b.get("type") == btype
            for k, b in self._blocks.items()
        )

    def finalize(self) -> SinkResult:
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,  # detector decides
            text="".join(self.accumulated_text),
            events=list(self.events),
            usage=dict(self.usage) if self.usage else None,
        )

    @property
    def subtype(self) -> str | None:
        return self._subtype

    @property
    def errors(self) -> list[str]:
        return list(self._errors)


__all__ = [
    "MAX_EVENT_BYTES",
    "MAX_EVENTS_PER_STEP",
    "SinkResult",
    "StreamingSink",
    "NoopSink",
    "ClaudeStreamSink",
]
