"""StreamingSink — consume one provider's stream event stream.

Phase 5: local streaming terminal output. Each provider that supports
streaming registers a `StreamingSink` implementation; providers that do not
register a `NoopSink` placeholder. `TaskExecutor` consumes JSON Lines from
the agent subprocess via the sink and prints text deltas in real time.

`ClaudeStreamSink` consumes Claude Code's ``stream-json`` line stream;
`CodexAppServerSink` drives ``codex app-server`` over JSON-RPC on stdio
(see :class:`CodexAppServerSink`).

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

    #: True when this sink drives the subprocess over **stdin** (a request/
    #: response protocol such as JSON-RPC) instead of only reading stdout.
    #: The executor then spawns with a stdin pipe and calls :meth:`begin`
    #: right after the spawn.
    wants_stdin: bool = False

    def __init__(self) -> None:
        self.accumulated_text: list[str] = []
        # Length of the cumulative text, maintained incrementally so every
        # event can stamp an exact ``text_offset`` without re-joining.
        self._text_len: int = 0
        # Optional observer for structured events (local CLI rendering).
        # NEVER used for the remote path — that rides progress/report.
        self.on_event: Callable[[dict[str, Any]], None] | None = None
        # Structured event buffer + monotonic sequence number. Shared by all
        # sink implementations so the wire shape stays identical per provider.
        self.events: deque[dict[str, Any]] = deque()
        self._seq: int = 0

    @abstractmethod
    def consume(self, raw: dict[str, Any]) -> None:
        """Process one parsed JSON event from the agent subprocess."""

    async def begin(self, stdin: Any, *, task: Any, work_dir: Any) -> None:
        """Hook for sinks that speak a request/response protocol on stdin.

        Called once after the subprocess spawns (and before stdout draining
        starts). ``stdin`` is the asyncio ``StreamWriter`` of the child
        process. The default implementation is a no-op.
        """
        return None

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


# ── Codex app-server (JSON-RPC 2.0 over stdio) ────────────────────────
#
# ``codex exec --json`` only emits block-level ``item.completed`` events
# (no deltas, no reasoning). The interactive protocol used by codex's own
# IDE clients is ``codex app-server``, a JSON-RPC v2 server whose
# notifications stream reasoning text, agent-message deltas, command
# output, per-call token usage, and typed ``ThreadItem`` lifecycles.
#
# The sink owns stdin: it performs the handshake (initialize /
# initialized / thread start|resume / turn start) from inside
# :meth:`consume` as responses arrive, then translates notifications into
# the same flat event wire format ``ClaudeStreamSink`` produces.

# Codex item types rendered as tool cards.
_CODEX_TOOL_ITEM_TYPES = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "webSearch",
        "plan",
        "collabAgentToolCall",
    }
)

_CODEX_TOOL_NAMES = {
    "commandExecution": "shell",
    "fileChange": "apply_patch",
    "webSearch": "web_search",
    "plan": "update_plan",
    "collabAgentToolCall": "agent",
}

# Server-initiated approval requests answered without a UI. Runs use
# ``approvalsReviewer=auto_review`` so these normally never fire; declining
# keeps a misconfigured run from hanging on a request nobody can answer.
_CODEX_APPROVAL_DECLINES = {
    "item/commandExecution/requestApproval": {"decision": "decline"},
    "item/fileChange/requestApproval": {"decision": "decline"},
}

# Fixed handshake request ids. The executor runs one turn per process, so
# ids never need to be recycled or correlated beyond these three steps.
_CODEX_INIT_REQUEST_ID = 1
_CODEX_THREAD_REQUEST_ID = 2
_CODEX_TURN_REQUEST_ID = 3


def _as_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _error_text(error: Any) -> str:
    """Render a JSON-RPC ``error`` object / string as one message."""
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message:
            return message
        return json.dumps(error, ensure_ascii=False)
    if isinstance(error, str):
        return error
    return ""


def _codex_usage_fields(breakdown: Any) -> dict[str, int]:
    """Map codex's camelCase token counters onto AGT's usage keys.

    Codex counts cached and cache-write input tokens *inside*
    ``inputTokens``; AGT stores billing-relevant non-cached input
    separately, so both cache buckets are subtracted.
    ``reasoningOutputTokens`` is already part of ``outputTokens`` and is
    not added twice.
    """
    if not isinstance(breakdown, dict):
        return {}
    cached = _as_int(breakdown.get("cachedInputTokens"))
    cache_write = _as_int(breakdown.get("cacheWriteInputTokens"))
    return {
        "input_tokens": max(
            0, _as_int(breakdown.get("inputTokens")) - cached - cache_write
        ),
        "output_tokens": _as_int(breakdown.get("outputTokens")),
        "cache_read_input_tokens": cached,
        "cache_creation_input_tokens": cache_write,
    }


def _reasoning_text(item: dict[str, Any]) -> str:
    """Full reasoning text from a completed ``reasoning`` item."""
    for key in ("content", "summary"):
        parts = item.get(key)
        if not isinstance(parts, list):
            continue
        text = "\n".join(str(part) for part in parts if part)
        if text:
            return text
    return ""


class CodexAppServerSink(StreamingSink):
    """Drive ``codex app-server --listen stdio://`` for one turn.

    Observed notifications (codex-cli 0.160.0, v2 protocol):

    - ``item/started`` / ``item/completed`` (``ThreadItem``) — lifecycle
    - ``item/agentMessage/delta`` — streamed assistant text
    - ``item/reasoning/textDelta`` / ``summaryTextDelta`` — thinking
    - ``item/commandExecution/outputDelta`` — live command output
    - ``thread/tokenUsage/updated`` — ``{total, last}`` per API call
    - ``turn/completed`` — terminal status (carries **no** usage)

    Text deltas are appended to :attr:`accumulated_text`; thinking, tool
    calls, tool results, and usage are emitted as structured events.
    ``turn/completed`` closes stdin so the app-server process exits and
    the executor's drain loop ends.
    """

    wants_stdin = True

    def __init__(
        self,
        *,
        sandbox: str = "workspace-write",
        approval_policy: str = "on-request",
        approvals_reviewer: str | None = "auto_review",
    ) -> None:
        super().__init__()
        # ``--approve-for-me`` parity: workspace-write sandbox plus
        # automatic review of approval requests. The reviewer runs on the
        # codex side, so a non-interactive run never waits on a TTY.
        self._sandbox = sandbox
        self._approval_policy = approval_policy
        self._approvals_reviewer = approvals_reviewer

        self._stdin: Any = None
        self._prompt = ""
        self._cwd = ""
        self._resume_id: str | None = None
        self._thread_id: str | None = None
        self._turn_id: str | None = None
        self._model: str | None = None
        self._resume_rejected = False
        self._is_error = False
        self._turn_status: str | None = None
        self._error_message: str | None = None
        self._finished = False
        self._retried_thread_setup = False
        self._exclude_turns = True

        self.usage: dict[str, Any] | None = None
        self._usage_totals = {key: 0 for key in _USAGE_KEYS}
        self._usage_calls = 0
        self._usage_emitted = False

        # Open tool cards keyed by item id (latest item payload).
        self._open_tools: dict[str, dict[str, Any]] = {}
        # Reasoning item ids with a rendered thinking block.
        self._thinking_started: set[str] = set()
        # item id -> "text" | "summary": the first delta channel wins so a
        # reasoning item is never rendered twice.
        self._reasoning_channel: dict[str, str] = {}
        # agentMessage item ids with text already appended.
        self._text_items: set[str] = set()
        self._emitted_text = False

    # ── Handshake (stdin side) ──────────────────────────────────

    async def begin(self, stdin: Any, *, task: Any, work_dir: Any) -> None:
        """Send ``initialize``/``initialized``; the rest follows responses."""
        self._stdin = stdin
        self._prompt = task.prompt
        self._resume_id = task.resume_session_id
        self._cwd = str(work_dir)
        self._send(
            {
                "jsonrpc": "2.0",
                "id": _CODEX_INIT_REQUEST_ID,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "agt",
                        "title": "agt",
                        "version": "0.1",
                    },
                    "capabilities": None,
                },
            }
        )
        self._send({"jsonrpc": "2.0", "method": "initialized"})
        try:
            await stdin.drain()
        except Exception:  # pragma: no cover - transport already gone
            log.debug("codex app-server stdin drain failed", exc_info=True)

    def _send(self, payload: dict[str, Any]) -> None:
        if self._stdin is None or self._finished:
            return
        try:
            self._stdin.write(
                (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            )
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            log.warning("codex app-server stdin write failed: %s", exc)
            self._is_error = True
            self._finished = True

    def _send_thread_setup(self) -> None:
        params: dict[str, Any] = {
            "cwd": self._cwd,
            "sandbox": self._sandbox,
            "approvalPolicy": self._approval_policy,
        }
        if self._approvals_reviewer:
            params["approvalsReviewer"] = self._approvals_reviewer
        if self._resume_id:
            # ``excludeTurns`` keeps a long thread's history out of the
            # response; the daemon only needs the thread id + model.
            params["threadId"] = self._resume_id
            if self._exclude_turns:
                params["excludeTurns"] = True
            method = "thread/resume"
        else:
            method = "thread/start"
        self._send(
            {
                "jsonrpc": "2.0",
                "id": _CODEX_THREAD_REQUEST_ID,
                "method": method,
                "params": params,
            }
        )

    def _send_turn_start(self) -> None:
        self._send(
            {
                "jsonrpc": "2.0",
                "id": _CODEX_TURN_REQUEST_ID,
                "method": "turn/start",
                "params": {
                    "threadId": self._thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": self._prompt,
                            "text_elements": [],
                        }
                    ],
                },
            }
        )

    # ── Public dispatch ─────────────────────────────────────────

    def consume(self, raw: dict[str, Any]) -> None:
        """Dispatch one JSON-RPC response, notification, or request."""
        method = raw.get("method")
        if isinstance(method, str):
            if "id" in raw:
                self._on_server_request(raw)
            else:
                self._on_notification(method, raw.get("params") or {})
            return
        if "id" in raw:
            self._on_response(raw)

    def _on_server_request(self, raw: dict[str, Any]) -> None:
        """Answer a server->client request without a UI.

        Approvals are declined; anything else (tool call, auth refresh)
        is rejected with a JSON-RPC error so the turn fails instead of
        hanging.
        """
        method = raw.get("method")
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": raw.get("id")}
        decline = _CODEX_APPROVAL_DECLINES.get(method)
        if decline is not None:
            reply["result"] = decline
        else:
            reply["error"] = {
                "code": -32601,
                "message": f"agt does not support {method}",
            }
        log.info("codex app-server request %s answered without a UI", method)
        self._send(reply)

    def _on_response(self, raw: dict[str, Any]) -> None:
        request_id = raw.get("id")
        error = raw.get("error")
        result = raw.get("result") or {}
        if request_id == _CODEX_INIT_REQUEST_ID:
            if error:
                self._fail(f"initialize failed: {_error_text(error)}")
                return
            self._send_thread_setup()
        elif request_id == _CODEX_THREAD_REQUEST_ID:
            if error:
                if self._maybe_retry_thread_setup(error):
                    return
                if self._resume_id:
                    self._resume_rejected = True
                self._fail(f"thread setup failed: {_error_text(error)}")
                return
            thread = result.get("thread") or {}
            thread_id = thread.get("id")
            if isinstance(thread_id, str):
                self._thread_id = thread_id
            model = result.get("model") or thread.get("model")
            if isinstance(model, str) and model:
                self._model = model
            self._send_turn_start()
        elif request_id == _CODEX_TURN_REQUEST_ID:
            if error:
                self._fail(f"turn/start failed: {_error_text(error)}")
                return
            turn = result.get("turn") or {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and not self._turn_id:
                self._turn_id = turn_id

    def _maybe_retry_thread_setup(self, error: Any) -> bool:
        """Retry once with only the baseline fields for older servers.

        ``approvalsReviewer`` and ``excludeTurns`` are newer than the v2
        protocol itself. A server that rejects them keeps working with an
        explicit ``never`` policy (approvals are refused rather than
        auto-reviewed) and full-history resume.
        """
        if self._retried_thread_setup:
            return False
        message = _error_text(error).lower()
        unsupported = (
            "approval",
            "excludeturns",
            "exclude_turns",
            "unknown variant",
            "unknown field",
            "unexpected",
        )
        if not any(token in message for token in unsupported):
            return False
        self._retried_thread_setup = True
        self._approvals_reviewer = None
        self._approval_policy = "never"
        self._exclude_turns = False
        self._send_thread_setup()
        return True

    # ── Notifications ───────────────────────────────────────────

    def _on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method == "turn/started":
            turn = params.get("turn") or {}
            turn_id = turn.get("id")
            if isinstance(turn_id, str) and not self._turn_id:
                self._turn_id = turn_id
        elif method == "item/started":
            self._on_item_started(params.get("item"))
        elif method == "item/completed":
            self._on_item_completed(params.get("item"))
        elif method == "item/agentMessage/delta":
            self._on_agent_delta(params)
        elif method == "item/reasoning/textDelta":
            self._on_reasoning_delta(params, "text")
        elif method == "item/reasoning/summaryTextDelta":
            self._on_reasoning_delta(params, "summary")
        elif method == "thread/tokenUsage/updated":
            self._on_token_usage(params)
        elif method == "turn/completed":
            self._on_turn_completed(params)
        elif method == "error":
            self._on_error(params)
        elif method in ("thread/started", "warning", "configWarning"):
            return
        else:
            log.debug("codex app-server notification ignored: %s", method)

    def _on_error(self, params: dict[str, Any]) -> None:
        message = _error_text(params.get("error"))
        if message:
            self._error_message = message
        if not params.get("willRetry"):
            self._is_error = True
        log.warning(
            "codex app-server error%s: %s",
            " (retrying)" if params.get("willRetry") else "",
            message,
        )

    # ── Items ───────────────────────────────────────────────────

    def _on_item_started(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        item_type = item.get("type")
        if item_type == "reasoning":
            self._thinking_started.add(item_id)
            self._emit("thinking_start", block_id=item_id)
        elif item_type in _CODEX_TOOL_ITEM_TYPES:
            self._open_tools[item_id] = item
            self._emit(
                "tool_use_start",
                block_id=item_id,
                tool_use_id=item_id,
                name=self._tool_name(item),
            )

    def _on_item_completed(self, item: Any) -> None:
        if not isinstance(item, dict):
            return
        item_id = item.get("id")
        if not isinstance(item_id, str):
            return
        item_type = item.get("type")
        if item_type == "reasoning":
            self._complete_reasoning(item)
        elif item_type in _CODEX_TOOL_ITEM_TYPES:
            self._complete_tool(item)
        elif item_type == "agentMessage":
            text = item.get("text")
            if isinstance(text, str) and text and item_id not in self._text_items:
                self._text_items.add(item_id)
                self._begin_message_text()
                self.on_text_delta(text)

    def _complete_reasoning(self, item: dict[str, Any]) -> None:
        item_id = item["id"]
        if item_id not in self._thinking_started:
            text = _reasoning_text(item)
            if text:
                self._thinking_started.add(item_id)
                self._emit("thinking_start", block_id=item_id)
                self._emit("thinking_delta", block_id=item_id, text=text)
        if item_id in self._thinking_started:
            self._emit("thinking_end", block_id=item_id)
            self._thinking_started.discard(item_id)

    def _complete_tool(self, item: dict[str, Any]) -> None:
        item_id = item["id"]
        name = self._tool_name(item)
        if self._open_tools.pop(item_id, None) is None:
            # Completion without a start notification (older servers or a
            # resumed turn replaying items): emit the opening card now.
            self._emit(
                "tool_use_start",
                block_id=item_id,
                tool_use_id=item_id,
                name=name,
            )
        self._emit(
            "tool_use_end",
            block_id=item_id,
            tool_use_id=item_id,
            name=name,
            input=self._tool_input(item),
        )
        content, is_error = self._tool_result(item)
        self._emit(
            "tool_result",
            tool_use_id=item_id,
            content=content,
            is_error=is_error,
        )

    def _on_agent_delta(self, params: dict[str, Any]) -> None:
        delta = params.get("delta")
        if not isinstance(delta, str) or not delta:
            return
        item_id = params.get("itemId")
        if isinstance(item_id, str) and item_id not in self._text_items:
            self._text_items.add(item_id)
            self._begin_message_text()
        self.on_text_delta(delta)

    def _begin_message_text(self) -> None:
        """Insert a paragraph break between successive agent messages."""
        if self._emitted_text:
            self.on_text_delta("\n\n")
        self._emitted_text = True

    def _on_reasoning_delta(self, params: dict[str, Any], channel: str) -> None:
        item_id = params.get("itemId")
        delta = params.get("delta")
        if not isinstance(item_id, str) or not isinstance(delta, str) or not delta:
            return
        seen = self._reasoning_channel.setdefault(item_id, channel)
        if seen != channel:
            return
        if item_id not in self._thinking_started:
            self._thinking_started.add(item_id)
            self._emit("thinking_start", block_id=item_id)
        self._emit("thinking_delta", block_id=item_id, text=delta)

    # ── Usage ───────────────────────────────────────────────────

    def _on_token_usage(self, params: dict[str, Any]) -> None:
        # On resume the server first replays the *previous* turn's usage;
        # only notifications stamped with this turn's id belong to us.
        if not self._turn_id or params.get("turnId") != self._turn_id:
            return
        breakdown = (params.get("tokenUsage") or {}).get("last") or {}
        fields = _codex_usage_fields(breakdown)
        if not fields:
            return
        self._usage_calls += 1
        for key, value in fields.items():
            self._usage_totals[key] += value
        fields["scope"] = "message"
        if self._model:
            fields["model"] = self._model
        self._emit("usage", **fields)

    def _emit_turn_usage(self) -> None:
        if self._usage_emitted or not self._usage_calls:
            return
        self._usage_emitted = True
        summary: dict[str, Any] = dict(self._usage_totals)
        summary["scope"] = "turn"
        summary["num_turns"] = self._usage_calls
        if self._model:
            summary["model"] = self._model
        self.usage = summary
        self._emit("usage", **summary)

    # ── Turn lifecycle ──────────────────────────────────────────

    def _on_turn_completed(self, params: dict[str, Any]) -> None:
        turn = params.get("turn") or {}
        self._turn_status = turn.get("status")
        if self._turn_status != "completed":
            self._is_error = True
            message = _error_text(turn.get("error"))
            if message:
                self._error_message = message
                log.warning("codex turn %s: %s", self._turn_status, message)
        self._emit_turn_usage()
        self._finish()

    def _fail(self, message: str) -> None:
        self._error_message = message
        self._is_error = True
        log.warning("codex app-server: %s", message)
        self._emit_turn_usage()
        self._finish()

    def _finish(self) -> None:
        """Close stdin so the app-server process exits after the turn."""
        if self._finished:
            return
        self._finished = True
        if self._stdin is not None:
            try:
                self._stdin.close()
            except Exception:  # pragma: no cover - transport already gone
                log.debug("codex app-server stdin close failed", exc_info=True)

    def _close_open_items(self) -> None:
        for item_id in list(self._thinking_started):
            self._emit("thinking_end", block_id=item_id)
        self._thinking_started.clear()
        for item_id, item in list(self._open_tools.items()):
            name = self._tool_name(item)
            self._emit(
                "tool_use_end",
                block_id=item_id,
                tool_use_id=item_id,
                name=name,
                input=self._tool_input(item),
            )
            self._emit(
                "tool_result",
                tool_use_id=item_id,
                content="",
                is_error=True,
            )
        self._open_tools.clear()

    # ── Item → event mapping ────────────────────────────────────

    @staticmethod
    def _tool_name(item: dict[str, Any]) -> str:
        item_type = item.get("type")
        if item_type == "mcpToolCall":
            return f"mcp:{item.get('server') or '?'}:{item.get('tool') or '?'}"
        if item_type == "dynamicToolCall":
            tool = item.get("tool") or "tool"
            namespace = item.get("namespace")
            return f"{namespace}:{tool}" if namespace else str(tool)
        return _CODEX_TOOL_NAMES.get(item_type, str(item_type))

    @staticmethod
    def _tool_input(item: dict[str, Any]) -> Any:
        item_type = item.get("type")
        if item_type == "commandExecution":
            return {"command": item.get("command"), "cwd": item.get("cwd")}
        if item_type == "fileChange":
            return {"changes": item.get("changes") or []}
        if item_type in ("mcpToolCall", "dynamicToolCall"):
            arguments = item.get("arguments")
            return arguments if isinstance(arguments, dict) else {
                "arguments": arguments
            }
        if item_type == "webSearch":
            return {"query": item.get("query"), "action": item.get("action")}
        if item_type == "plan":
            return {"plan": item.get("text")}
        if item_type == "collabAgentToolCall":
            return {"tool": item.get("tool"), "prompt": item.get("prompt")}
        return {}

    @staticmethod
    def _tool_result(item: dict[str, Any]) -> tuple[str, bool]:
        item_type = item.get("type")
        if item_type == "commandExecution":
            status = item.get("status")
            exit_code = item.get("exitCode")
            is_error = status in ("failed", "declined") or (
                status == "completed"
                and isinstance(exit_code, int)
                and exit_code != 0
            )
            return item.get("aggregatedOutput") or "", is_error
        if item_type == "fileChange":
            lines = []
            for change in item.get("changes") or []:
                if not isinstance(change, dict):
                    continue
                kind = change.get("kind")
                if isinstance(kind, dict):
                    kind = kind.get("type")
                header = f"{kind or 'update'}: {change.get('path')}"
                diff = change.get("diff")
                lines.append(
                    f"{header}\n{diff}"
                    if isinstance(diff, str) and diff
                    else header
                )
            return "\n".join(lines), item.get("status") in ("failed", "declined")
        if item_type == "mcpToolCall":
            if item.get("error"):
                return _content_text(item.get("error")), True
            return _content_text(item.get("result")), item.get("status") == "failed"
        if item_type == "dynamicToolCall":
            is_error = item.get("success") is False or item.get("status") == "failed"
            return _content_text(item.get("contentItems")), is_error
        if item_type == "webSearch":
            return _content_text(item.get("results")), False
        return "", False

    # ── Terminal result ─────────────────────────────────────────

    def finalize(self) -> SinkResult:
        self._emit_turn_usage()
        self._close_open_items()
        return SinkResult(
            session_id=self._thread_id,
            is_error=self._is_error,
            resume_rejected=self._resume_rejected,
            text="".join(self.accumulated_text),
            events=list(self.events),
            usage=dict(self.usage) if self.usage else None,
        )

    @property
    def subtype(self) -> str | None:
        return self._turn_status

    @property
    def errors(self) -> list[str]:
        return [self._error_message] if self._error_message else []


def _opencode_usage_fields(tokens: Any) -> dict[str, int]:
    """Map OpenCode ``step_finish.tokens`` onto AGT's usage keys.

    Observed shape (opencode 2.2.5)::

        {"input": 778, "output": 18, "reasoning": 34,
         "cache": {"read": 4352, "write": 0}}

    ``input`` already excludes cache reads/writes, so no subtraction is
    needed (unlike Codex). ``reasoning`` is not part of the AGT usage
    schema and is left out of billing counters.
    """
    if not isinstance(tokens, dict):
        return {}
    cache = tokens.get("cache")
    if not isinstance(cache, dict):
        cache = {}
    return {
        "input_tokens": _as_int(tokens.get("input")),
        "output_tokens": _as_int(tokens.get("output")),
        "cache_read_input_tokens": _as_int(cache.get("read")),
        "cache_creation_input_tokens": _as_int(cache.get("write")),
    }


class OpenCodeStreamSink(StreamingSink):
    """Consume OpenCode ``run --format json`` JSON-Lines events.

    Observed event shapes (opencode 2.2.5, ``opencode run --format json``):

    - ``{"type":"step_start","sessionID":"ses_...","part":{...}}``
    - ``{"type":"text","sessionID":"ses_...","part":{"id","type":"text",
      "text","time"}}`` — one complete text part per event (no deltas)
    - ``{"type":"reasoning",...,"part":{"id","type":"reasoning","text"}}``
      — only emitted when ``--thinking`` is passed
    - ``{"type":"tool_use",...,"part":{"id","tool","state":{"status",
      "input","output"|"error",...}}}`` — already completed/failed
    - ``{"type":"step_finish",...,"part":{"reason","cost","tokens":{...}}}``
    - ``{"type":"error",...,"error":{"type","message"}}`` — the CLI also
      exits non-zero, but trusting the in-band error keeps the status
      independent of the exit code

    Every event carries the top-level ``sessionID``, so the session
    pointer never needs the (SQLite-backed) disk fallback. Text parts are
    printed as they arrive; successive parts are separated by a blank
    line because OpenCode emits whole parts rather than token deltas.
    """

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None
        self._is_error: bool = False
        self._error_message: str | None = None
        # ``part id -> text already emitted`` so a replayed/suffix text
        # event can never duplicate the visible output.
        self._text_parts: dict[str, str] = {}
        self._emitted_text: bool = False
        # Reasoning parts already rendered, keyed by part id.
        self._thinking_parts: set[str] = set()
        # Tool calls already rendered (tool_use events are terminal).
        self._tool_parts: set[str] = set()
        self._usage_calls: int = 0
        self._usage_totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self._cost_total: float = 0.0
        self._usage_emitted: bool = False
        self.usage: dict[str, Any] | None = None

    def consume(self, raw: dict[str, Any]) -> None:
        """Dispatch on event ``type``; unknown types are silently skipped."""
        if not isinstance(raw, dict):
            return
        session_id = raw.get("sessionID")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
        mtype = raw.get("type")
        if mtype == "text":
            self._consume_text(raw.get("part") or {})
        elif mtype == "reasoning":
            self._consume_reasoning(raw.get("part") or {})
        elif mtype == "tool_use":
            self._consume_tool(raw.get("part") or {})
        elif mtype == "step_finish":
            self._consume_step_finish(raw.get("part") or {})
        elif mtype == "error":
            self._consume_error(raw.get("error"))

    def _consume_text(self, part: dict[str, Any]) -> None:
        text = part.get("text")
        if not isinstance(text, str) or not text:
            return
        part_id = str(part.get("id") or "")
        previous = self._text_parts.get(part_id, "")
        if previous and text.startswith(previous):
            # Reconcile replay: only the not-yet-rendered suffix is new.
            text = text[len(previous):]
            if not text:
                return
        elif part_id in self._text_parts:
            return
        self._text_parts[part_id] = previous + text
        if not previous and self._emitted_text:
            # Separate distinct message parts; never split a suffixed part.
            self.on_text_delta("\n\n")
        self.on_text_delta(text)
        self._emitted_text = True

    def _consume_reasoning(self, part: dict[str, Any]) -> None:
        text = part.get("text")
        part_id = str(part.get("id") or "")
        if not isinstance(text, str) or not text or part_id in self._thinking_parts:
            return
        self._thinking_parts.add(part_id)
        self._emit("thinking_start", block_id=part_id)
        self._emit("thinking_delta", block_id=part_id, text=text)
        self._emit("thinking_end", block_id=part_id)

    def _consume_tool(self, part: dict[str, Any]) -> None:
        part_id = str(part.get("id") or "")
        state = part.get("state") if isinstance(part.get("state"), dict) else {}
        status = state.get("status")
        if status not in ("completed", "error") or part_id in self._tool_parts:
            return
        self._tool_parts.add(part_id)
        name = str(part.get("tool") or "")
        self._emit(
            "tool_use_start",
            block_id=part_id,
            tool_use_id=part_id,
            name=name,
        )
        self._emit(
            "tool_use_end",
            block_id=part_id,
            tool_use_id=part_id,
            name=name,
            input=state.get("input") or {},
        )
        is_error = status == "error"
        content = state.get("error") if is_error else state.get("output")
        self._emit(
            "tool_result",
            tool_use_id=part_id,
            content=_content_text(content),
            is_error=is_error,
        )

    def _consume_step_finish(self, part: dict[str, Any]) -> None:
        fields = _opencode_usage_fields(part.get("tokens"))
        cost = part.get("cost")
        if isinstance(cost, (int, float)) and not isinstance(cost, bool):
            self._cost_total += float(cost)
        if not fields:
            return
        self._usage_calls += 1
        for key, value in fields.items():
            self._usage_totals[key] += value
        fields["scope"] = "message"
        if self._cost_total:
            fields["cost_usd"] = round(self._cost_total, 6)
        self._emit("usage", **fields)

    def _consume_error(self, error: Any) -> None:
        self._is_error = True
        message = _error_text(error)
        if message:
            self._error_message = message
            log.warning("opencode: %s", message)

    def _emit_turn_usage(self) -> None:
        if self._usage_emitted or not self._usage_calls:
            return
        self._usage_emitted = True
        summary: dict[str, Any] = dict(self._usage_totals)
        summary["scope"] = "turn"
        summary["num_turns"] = self._usage_calls
        if self._cost_total:
            summary["cost_usd"] = round(self._cost_total, 6)
        self.usage = summary
        self._emit("usage", **summary)

    def finalize(self) -> SinkResult:
        self._emit_turn_usage()
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,
            text="".join(self.accumulated_text),
            events=list(self.events),
            usage=dict(self.usage) if self.usage else None,
        )

    @property
    def subtype(self) -> str | None:
        return None

    @property
    def errors(self) -> list[str]:
        return [self._error_message] if self._error_message else []


def _pi_usage_fields(usage: Any) -> dict[str, int]:
    """Map Pi ``Usage`` onto AGT's usage keys.

    Pi reports ``input`` / ``output`` / ``cacheRead`` / ``cacheWrite``
    (already separated, like OpenCode). ``reasoning`` is included in
    ``output`` and must not be added again.
    """
    if not isinstance(usage, dict):
        return {}
    return {
        "input_tokens": _as_int(usage.get("input")),
        "output_tokens": _as_int(usage.get("output")),
        "cache_read_input_tokens": _as_int(usage.get("cacheRead")),
        "cache_creation_input_tokens": _as_int(usage.get("cacheWrite")),
    }


def _pi_cost(usage: Any) -> float:
    """Total cost from Pi's nested ``cost.total`` (0.0 when absent)."""
    if not isinstance(usage, dict):
        return 0.0
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return 0.0
    total = cost.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return 0.0
    return float(total)


def _pi_result_text(result: Any) -> str:
    """Render a Pi tool result's ``content`` blocks as text."""
    if isinstance(result, dict):
        content = result.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            chunks = [
                block["text"]
                for block in content
                if isinstance(block, dict)
                and block.get("type") == "text"
                and isinstance(block.get("text"), str)
            ]
            if chunks:
                return "\n".join(chunks)
    return _content_text(result)


class PiStreamSink(StreamingSink):
    """Consume Pi's ``--mode json`` JSON-Lines event stream.

    Observed shape (pi 1.4.0, session format v3):

    - ``{"type":"session","version":3,"id":"...","cwd":"..."}`` — header
    - ``{"type":"message_start|message_end","message":{...}}`` — full
      messages; the header's ``id`` is the session pointer
    - ``{"type":"message_update","assistantMessageEvent":{...}}`` —
      delta-only stream (``text_delta`` / ``thinking_delta`` /
      ``toolcall_start|delta|end``), with the event's ``contentIndex``
      identifying the block
    - ``tool_execution_start|update|end`` — tool lifecycle; the end
      carries the result and ``isError``
    - ``auto_retry_end`` / ``agent_settled`` — terminal failure signals

    JSON mode exits 0 even when the model call fails, so
    ``message_end.stopReason == "error"`` and ``auto_retry_end.success
    == false`` are authoritative error signals. ``message_end.message``
    is the final authority for text and usage; deltas are reconciled
    against it so a replay or a provider that skips deltas never
    duplicates or loses visible text.
    """

    def __init__(self) -> None:
        super().__init__()
        self._session_id: str | None = None
        self._is_error: bool = False
        self._error_message: str | None = None
        self._message_seq: int = 0
        self._current_message_key: str | None = None
        self._text_started_this_message: bool = False
        self._emitted_text: bool = False
        # block key -> text already printed
        self._text_blocks: dict[str, str] = {}
        # block key -> thinking text already emitted
        self._thinking_blocks: dict[str, str] = {}
        self._open_thinking: set[str] = set()
        # toolCallId -> {"block_id", "name", "input", "started", "ended"}
        self._tool_calls: dict[str, dict[str, Any]] = {}
        self._tool_results: set[str] = set()
        self._usage_calls: int = 0
        self._usage_totals: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
        self._cost_total: float = 0.0
        self._usage_emitted: bool = False
        self.usage: dict[str, Any] | None = None

    # ── Public dispatch ─────────────────────────────────────────

    def consume(self, raw: dict[str, Any]) -> None:
        """Dispatch on event ``type``; unknown types are silently skipped."""
        if not isinstance(raw, dict):
            return
        mtype = raw.get("type")
        if mtype == "session":
            session_id = raw.get("id")
            if isinstance(session_id, str) and session_id:
                self._session_id = session_id
        elif mtype == "message_start":
            self._on_message_start(raw.get("message") or {})
        elif mtype == "message_update":
            self._on_message_update(raw.get("assistantMessageEvent") or {})
        elif mtype == "message_end":
            self._on_message_end(raw.get("message") or {})
        elif mtype == "tool_execution_start":
            self._on_tool_execution_start(raw)
        elif mtype == "tool_execution_end":
            self._on_tool_execution_end(raw)
        elif mtype == "auto_retry_end":
            if raw.get("success") is False:
                self._fail(str(raw.get("finalError") or "retry failed"))
        elif mtype == "agent_settled":
            if raw.get("aborted") is True:
                self._fail("request aborted")

    # ── Messages ────────────────────────────────────────────────

    def _on_message_start(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        self._message_seq += 1
        self._current_message_key = f"pi-{self._message_seq}"
        self._text_started_this_message = False

    def _on_message_update(self, event: dict[str, Any]) -> None:
        if not isinstance(event, dict):
            return
        key = self._block_key(event.get("contentIndex"))
        etype = event.get("type")
        if etype == "text_start":
            return
        if etype == "text_delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._append_text(key, delta)
        elif etype == "text_end":
            content = event.get("content")
            if isinstance(content, str):
                self._reconcile_text(key, content)
        elif etype == "thinking_start":
            self._begin_thinking(key)
        elif etype == "thinking_delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                self._begin_thinking(key)
                self._thinking_blocks[key] = (
                    self._thinking_blocks.get(key, "") + delta
                )
                self._emit("thinking_delta", block_id=key, text=delta)
        elif etype == "thinking_end":
            content = event.get("content")
            if key not in self._open_thinking:
                text = content if isinstance(content, str) else ""
                if text:
                    self._begin_thinking(key)
                    self._thinking_blocks[key] = text
                    self._emit("thinking_delta", block_id=key, text=text)
            self._end_thinking(key)
        elif etype == "toolcall_start":
            self._begin_tool(key, event.get("id"), event.get("toolName"))
        elif etype == "toolcall_end":
            tool_call = event.get("toolCall")
            if not isinstance(tool_call, dict):
                return
            tool_id = tool_call.get("id") or event.get("id")
            info = self._begin_tool(
                key,
                tool_id,
                tool_call.get("name") or event.get("toolName"),
            )
            self._end_tool(info, tool_call.get("arguments") or {})
        elif etype == "error":
            message = event.get("error")
            if message:
                self._fail(str(message))

    def _on_message_end(self, message: dict[str, Any]) -> None:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            return
        key_prefix = self._current_message_key or "?"

        stop_reason = message.get("stopReason")
        if stop_reason in ("error", "aborted"):
            self._fail(
                str(message.get("errorMessage") or f"request {stop_reason}")
            )
        self._on_message_usage(message.get("usage"))
        self._render_fallback_content(message.get("content"), key_prefix)

    def _render_fallback_content(self, content: Any, key_prefix: str) -> None:
        """Reconcile the authoritative message content with streamed blocks."""
        if not isinstance(content, list):
            return
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            key = f"{key_prefix}:{index}"
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str):
                    self._reconcile_text(key, text)
            elif block_type == "thinking":
                # Already streamed or emitted; only recover a block that
                # produced no deltas at all.
                if key not in self._thinking_blocks:
                    text = block.get("thinking")
                    if isinstance(text, str) and text:
                        self._begin_thinking(key)
                        self._thinking_blocks[key] = text
                        self._emit("thinking_delta", block_id=key, text=text)
                self._end_thinking(key)
            elif block_type in ("toolCall", "tool_call"):
                tool_id = block.get("id")
                info = self._begin_tool(
                    key, tool_id, block.get("name") or ""
                )
                self._end_tool(info, block.get("arguments") or {})

    # ── Text ────────────────────────────────────────────────────

    def _append_text(self, key: str, text: str) -> None:
        if not self._text_started_this_message:
            if self._emitted_text:
                self.on_text_delta("\n\n")
            self._text_started_this_message = True
        self._emitted_text = True
        self._text_blocks[key] = self._text_blocks.get(key, "") + text
        self.on_text_delta(text)

    def _reconcile_text(self, key: str, content: str) -> None:
        emitted = self._text_blocks.get(key, "")
        if content.startswith(emitted):
            suffix = content[len(emitted):]
            if suffix:
                self._append_text(key, suffix)
            return
        if not emitted and content:
            self._append_text(key, content)

    # ── Thinking ────────────────────────────────────────────────

    def _begin_thinking(self, key: str) -> None:
        if key in self._open_thinking:
            return
        self._open_thinking.add(key)
        self._emit("thinking_start", block_id=key)

    def _end_thinking(self, key: str) -> None:
        if key not in self._open_thinking:
            return
        self._open_thinking.discard(key)
        self._emit("thinking_end", block_id=key)

    # ── Tools ───────────────────────────────────────────────────

    def _begin_tool(
        self,
        key: str,
        tool_id: Any,
        name: Any,
    ) -> dict[str, Any]:
        identifier = tool_id if isinstance(tool_id, str) and tool_id else key
        info = self._tool_calls.get(identifier)
        if info is None:
            info = {
                "id": identifier,
                "block_id": key,
                "name": name if isinstance(name, str) else "",
                "input": {},
                "started": False,
                "ended": False,
            }
            self._tool_calls[identifier] = info
        elif isinstance(name, str) and name and not info["name"]:
            info["name"] = name
        if not info["started"]:
            info["started"] = True
            self._emit(
                "tool_use_start",
                block_id=info["block_id"],
                tool_use_id=identifier,
                name=info["name"],
            )
        return info

    def _end_tool(self, info: dict[str, Any], arguments: Any) -> None:
        if info.get("ended"):
            return
        info["ended"] = True
        if isinstance(arguments, dict):
            info["input"] = arguments
        self._emit(
            "tool_use_end",
            block_id=info["block_id"],
            tool_use_id=info["id"],
            name=info["name"],
            input=info["input"],
        )

    def _on_tool_execution_start(self, raw: dict[str, Any]) -> None:
        tool_id = raw.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id:
            return
        info = self._begin_tool(
            tool_id, tool_id, raw.get("toolName") or ""
        )
        arguments = raw.get("args")
        if isinstance(arguments, dict):
            info["input"] = arguments

    def _on_tool_execution_end(self, raw: dict[str, Any]) -> None:
        tool_id = raw.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id:
            return
        info = self._begin_tool(
            tool_id, tool_id, raw.get("toolName") or ""
        )
        arguments = raw.get("args")
        if isinstance(arguments, dict):
            info["input"] = arguments
        self._end_tool(info, info["input"])
        if tool_id in self._tool_results:
            return
        self._tool_results.add(tool_id)
        self._emit(
            "tool_result",
            tool_use_id=tool_id,
            content=_pi_result_text(raw.get("result")),
            is_error=bool(raw.get("isError")),
        )

    # ── Usage ───────────────────────────────────────────────────

    def _on_message_usage(self, usage: Any) -> None:
        fields = _pi_usage_fields(usage)
        cost = _pi_cost(usage)
        if not fields or not (any(fields.values()) or cost):
            return
        self._usage_calls += 1
        for key, value in fields.items():
            self._usage_totals[key] += value
        self._cost_total += cost
        fields["scope"] = "message"
        if cost:
            fields["cost_usd"] = round(self._cost_total, 6)
        self._emit("usage", **fields)

    def _emit_turn_usage(self) -> None:
        if self._usage_emitted or not self._usage_calls:
            return
        self._usage_emitted = True
        summary: dict[str, Any] = dict(self._usage_totals)
        summary["scope"] = "turn"
        summary["num_turns"] = self._usage_calls
        if self._cost_total:
            summary["cost_usd"] = round(self._cost_total, 6)
        self.usage = summary
        self._emit("usage", **summary)

    # ── Helpers / terminal ──────────────────────────────────────

    def _block_key(self, index: Any) -> str:
        return f"{self._current_message_key or '?'}:{index}"

    def _fail(self, message: str) -> None:
        self._is_error = True
        if message:
            self._error_message = message
            log.warning("pi: %s", message)

    def _close_open_blocks(self) -> None:
        for key in list(self._open_thinking):
            self._end_thinking(key)
        for tool_id, info in self._tool_calls.items():
            if not info.get("ended"):
                self._end_tool(info, info["input"])
            if tool_id not in self._tool_results:
                self._tool_results.add(tool_id)
                self._emit(
                    "tool_result",
                    tool_use_id=tool_id,
                    content="",
                    is_error=True,
                )

    def finalize(self) -> SinkResult:
        self._close_open_blocks()
        self._emit_turn_usage()
        return SinkResult(
            session_id=self._session_id,
            is_error=self._is_error,
            resume_rejected=False,
            text="".join(self.accumulated_text),
            events=list(self.events),
            usage=dict(self.usage) if self.usage else None,
        )

    @property
    def subtype(self) -> str | None:
        return "error" if self._is_error else None

    @property
    def errors(self) -> list[str]:
        return [self._error_message] if self._error_message else []


__all__ = [
    "MAX_EVENT_BYTES",
    "MAX_EVENTS_PER_STEP",
    "SinkResult",
    "StreamingSink",
    "NoopSink",
    "ClaudeStreamSink",
    "CodexAppServerSink",
    "OpenCodeStreamSink",
    "PiStreamSink",
]
