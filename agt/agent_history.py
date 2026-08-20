"""Agent conversation history readers.

Each provider stores its own session transcript. We expose a uniform
``load_history(provider, session_id)`` so the CLI's ``chat --resume`` can
print the past turns without re-running the agent.

Scope of Phase 4 workdir / chat-multisession:
- Claude Code: full reader (reads ``~/.claude/projects/<hash>/<sid>.jsonl``)
- Codex CLI: full reader (reads ``~/.codex/sessions/**/rollout-*.jsonl``)
- cursor / others: stubbed with ``NotImplementedError`` (Phase 5+)

The reader is read-only and best-effort: if the user deleted their
``~/.claude`` / ``~/.codex`` folders, we return an empty list rather than
crash — the CLI then prints "no history available" and proceeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass
class Message:
    """A single message in a provider session transcript."""

    role: str  # "user" | "assistant" | "system"
    content: str  # text content (concatenated if multi-block)
    timestamp: float | None = None


def load_claude_history(session_id: str) -> list[Message]:
    """Read Claude Code's transcript for ``session_id``.

    Claude Code stores each session as a ``<uuid>.jsonl`` file inside
    ``~/.claude/projects/<project-hash>/``. We scan every project dir
    looking for a file whose stem matches ``session_id``.

    Returns an empty list if the file is gone (e.g. user wiped their
    cache) — callers handle that gracefully.
    """
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return []
    target_stem = session_id
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{target_stem}.jsonl"
        if candidate.exists():
            return _parse_jsonl(candidate)
    return []


def _parse_jsonl(path: Path) -> list[Message]:
    """Parse a Claude Code ``.jsonl`` transcript into ``Message`` objects.

    Claude Code's line shape (per Anthropic's docs and observed output):

    ```
    {"type":"user","message":{"role":"user","content":"..."},      "timestamp":"..."}
    {"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"..."}, ...]}, "timestamp":"..."}
    ```

    Content can be a plain string (user) or a list of blocks (assistant).
    We flatten both to a single ``content`` string for terminal display.
    """
    out: list[Message] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = _extract_message(obj)
        if msg is not None:
            out.append(msg)
    return out


def _extract_message(obj: dict) -> Message | None:
    """Pull ``(role, content, timestamp)`` out of one Claude Code line.

    Tolerates the two shapes observed in the wild (string ``content`` vs
    block list ``content``) and silently skips lines that don't look
    like a message (tool results, file-history snapshots, etc).
    """
    obj_type = obj.get("type")
    if obj_type not in ("user", "assistant"):
        # system / tool_result / file-history-snapshot — skip silently
        return None
    inner = obj.get("message") or {}
    role = inner.get("role") or obj_type
    content = inner.get("content")
    flat = _flatten_content(content)
    if flat is None:
        return None
    ts = _parse_timestamp(obj.get("timestamp"))
    return Message(role=role, content=flat, timestamp=ts)


def _parse_timestamp(raw: object) -> float | None:
    """Coerce a numeric or ISO-8601 timestamp to epoch seconds.

    Claude Code lines carry an ISO ``timestamp`` string (sometimes a
    numeric); Codex rollouts carry ISO-8601 with a ``Z`` suffix. We
    accept both, returning ``None`` on anything unrecognised.
    """
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None
    return None


def _flatten_content(content: object | None) -> str | None:
    """Coerce a Claude Code ``content`` field to a single string.

    - ``str`` → returned as-is
    - ``list`` of ``{"type": "text", "text": "..."}`` blocks → joined
    - anything else (None, image blocks, tool_use) → ``None``
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        joined = "\n".join(chunks)
        return joined or None
    return None


def load_codex_history(session_id: str) -> list[Message]:
    """Read Codex CLI's transcript for ``session_id``.

    Codex stores one ``rollout-<ts>-<uuid>.jsonl`` per session under
    ``~/.codex/sessions/<YYYY>/<MM>/<DD>/`` (archived sessions move to
    ``~/.codex/archived_sessions/``). The session uuid is the last
    hyphen-separated component of the file stem, so we match by
    ``stem.endswith(session_id)``.

    Returns an empty list if the file is gone — callers handle that
    gracefully.
    """
    home = Path.home()
    roots = (home / ".codex" / "sessions", home / ".codex" / "archived_sessions")
    matches: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if path.stem.endswith(session_id):
                matches.append(path)
    if not matches:
        return []
    matches.sort(key=_mtime, reverse=True)
    return _parse_codex_jsonl(matches[0])


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _parse_codex_jsonl(path: Path) -> list[Message]:
    """Parse a Codex ``rollout-*.jsonl`` into ``Message`` objects.

    Each line is ``{"timestamp": ..., "type": ..., "payload": {...}}``.
    We keep only ``response_item`` lines whose payload is a
    ``message`` (``role`` = user/assistant); tool calls, reasoning and
    lifecycle events are skipped.
    """
    out: list[Message] = []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            msg = _extract_codex_message(obj)
            if msg is not None:
                out.append(msg)
    return out


def _extract_codex_message(obj: dict) -> Message | None:
    """Pull ``(role, content, timestamp)`` out of a Codex ``response_item``."""
    if obj.get("type") != "response_item":
        return None
    payload = obj.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in ("user", "assistant"):
        return None
    flat = _flatten_codex_content(payload.get("content"))
    if flat is None:
        return None
    return Message(
        role=role,
        content=flat,
        timestamp=_parse_timestamp(obj.get("timestamp")),
    )


def _flatten_codex_content(content: object | None) -> str | None:
    """Coerce a Codex ``content`` list to a single string.

    - ``str`` → returned as-is
    - ``list`` of ``{"type": "input_text"|"output_text"|"text", "text": ...}`` → joined
    - anything else (None, tool_call, reasoning) → ``None``
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if (
                isinstance(block, dict)
                and block.get("type") in ("input_text", "output_text", "text")
            ):
                text = block.get("text")
                if isinstance(text, str):
                    chunks.append(text)
        return "\n".join(chunks) or None
    return None


def load_history(provider: str, session_id: str) -> list[Message]:
    """Provider-dispatched history loader."""
    if provider == "claude":
        return load_claude_history(session_id)
    if provider == "codex":
        return load_codex_history(session_id)
    raise NotImplementedError(
        f"history reader for provider '{provider}' is not yet implemented"
    )


__all__ = [
    "Message",
    "load_history",
    "load_claude_history",
    "load_codex_history",
]