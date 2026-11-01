"""Agent conversation history readers.

Each provider stores its own session transcript. We expose a uniform
``load_history(provider, session_id)`` so the CLI's ``chat --resume`` can
print the past turns without re-running the agent.

Scope of Phase 4 workdir / chat-multisession:
- Claude Code: full reader (reads ``~/.claude/projects/<hash>/<sid>.jsonl``)
- Codex CLI: full reader (reads ``~/.codex/sessions/**/rollout-*.jsonl``)
- OpenCode: full reader (reads ``opencode.db`` under the XDG data dir)
- cursor / others: stubbed with ``NotImplementedError`` (Phase 5+)

The reader is read-only and best-effort: if the user deleted their
``~/.claude`` / ``~/.codex`` folders (or OpenCode's database), we return an
empty list rather than crash — the CLI then prints "no history available"
and proceeds.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

# OpenCode keeps sessions in SQLite (``opencode.db``), not per-session JSONL
# files. ``opencode debug paths`` reports the same location; honoring
# ``XDG_DATA_HOME`` matches its own resolution.
_OPENCODE_DB_ENV = "AGT_OPENCODE_DB"
_OPENCODE_BIN_ENV = "AGT_OPENCODE_BIN"
_OPENCODE_DB_TIMEOUT_SECONDS = 2.0
_OPENCODE_DELETE_TIMEOUT_SECONDS = 30.0


@dataclass
class Message:
    """A single message in a provider session transcript."""

    role: str  # "user" | "assistant" | "system"
    content: str  # text content (concatenated if multi-block)
    timestamp: float | None = None


@dataclass(frozen=True)
class ProviderSessionDeleteResult:
    """Result of deleting provider-local files for one conversation."""

    supported: bool
    deleted_paths: tuple[Path, ...] = ()
    missing: bool = False
    errors: tuple[str, ...] = ()
    reason: str | None = None


def _validate_session_id(session_id: str) -> None:
    if not session_id or any(char in session_id for char in "/\\"):
        raise ValueError("invalid provider session id")


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


def _opencode_db_path() -> Path:
    """Locate OpenCode's SQLite database.

    ``AGT_OPENCODE_DB`` overrides the path (tests, custom installs). The
    default follows OpenCode's own data root: ``$XDG_DATA_HOME/opencode``
    when set, otherwise ``~/.local/share/opencode`` on macOS and Linux.
    """
    override = os.environ.get(_OPENCODE_DB_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg_data).expanduser() if xdg_data else Path.home() / ".local" / "share"
    return base / "opencode" / "opencode.db"


def load_opencode_history(session_id: str) -> list[Message]:
    """Read OpenCode's transcript for ``session_id``.

    OpenCode 2.x stores sessions in SQLite rather than one file per
    session. ``session_message`` holds the assembled turns: ``user`` rows
    carry ``data.text``; ``assistant`` rows carry ``data.content`` blocks
    (``text``, ``reasoning``, ``tool``). ``session_part`` is empty in
    observed 2.2.5 databases, so messages can be read in one query.

    A read-only connection sees a consistent snapshot even while OpenCode
    is writing. Missing database, missing table, or a locked/corrupt file
    all return an empty list, matching the best-effort contract of the
    other readers.
    """
    _validate_session_id(session_id)
    db_path = _opencode_db_path()
    if not db_path.is_file():
        return []
    try:
        rows = _query_opencode_messages(db_path, session_id)
    except (OSError, sqlite3.Error):
        return []
    out: list[Message] = []
    for message_type, raw in rows:
        msg = _extract_opencode_message(message_type, raw)
        if msg is not None:
            out.append(msg)
    return out


def _open_opencode_readonly(db_path: Path) -> sqlite3.Connection:
    """Open OpenCode's database read-only with a short busy timeout."""
    uri = f"{db_path.resolve(strict=False).as_uri()}?mode=ro"
    connection = sqlite3.connect(
        uri,
        uri=True,
        timeout=_OPENCODE_DB_TIMEOUT_SECONDS,
    )
    connection.execute(
        f"PRAGMA busy_timeout = {int(_OPENCODE_DB_TIMEOUT_SECONDS * 1000)}"
    )
    return connection


def _opencode_session_exists(db_path: Path, session_id: str) -> bool:
    """Whether ``session_id`` has a row in ``session_v2``."""
    connection = _open_opencode_readonly(db_path)
    try:
        return (
            connection.execute(
                "SELECT 1 FROM session_v2 WHERE id = ? LIMIT 1",
                (session_id,),
            ).fetchone()
            is not None
        )
    finally:
        connection.close()


def _query_opencode_messages(
    db_path: Path,
    session_id: str,
) -> list[tuple[str, object]]:
    """Fetch the user/assistant rows of one session in order."""
    connection = _open_opencode_readonly(db_path)
    try:
        return connection.execute(
            "SELECT type, data FROM session_message "
            "WHERE session_id = ? AND type IN ('user', 'assistant') "
            "ORDER BY seq",
            (session_id,),
        ).fetchall()
    finally:
        connection.close()


def _extract_opencode_message(message_type: str, raw: object) -> Message | None:
    """Pull ``(role, content, timestamp)`` out of one ``session_message`` row."""
    if not isinstance(raw, str):
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    if message_type == "user":
        text = data.get("text")
    else:
        text = _flatten_opencode_content(data.get("content"))
    if not isinstance(text, str) or not text:
        return None
    return Message(
        role=message_type,
        content=text,
        timestamp=_opencode_timestamp(data.get("time")),
    )


def _flatten_opencode_content(content: object) -> str | None:
    """Join the ``text`` blocks of an assistant message.

    Observed block types are ``text``, ``reasoning`` and ``tool``; only
    ``text`` is user-visible turn content. This mirrors the Claude reader,
    which drops reasoning and tool_use blocks.
    """
    if isinstance(content, str):
        return content or None
    if not isinstance(content, list):
        return None
    chunks: list[str] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if isinstance(text, str):
            chunks.append(text)
    joined = "\n".join(chunks)
    return joined or None


def _opencode_timestamp(time_value: object) -> float | None:
    """Convert OpenCode's ``time.created`` epoch milliseconds to seconds."""
    if not isinstance(time_value, dict):
        return None
    created = time_value.get("created")
    if isinstance(created, bool) or not isinstance(created, (int, float)):
        return None
    return float(created) / 1000.0


def find_opencode_session_files(session_id: str) -> list[Path]:
    """Locate the OpenCode database that holds ``session_id``.

    OpenCode keeps every session in one SQLite file, so the returned path
    is the shared container rather than a per-session transcript. Callers
    use it for existence checks and dry-run reporting only; deletion never
    unlinks this file.
    """
    _validate_session_id(session_id)
    db_path = _opencode_db_path()
    if not db_path.is_file():
        return []
    try:
        exists = _opencode_session_exists(db_path, session_id)
    except (OSError, sqlite3.Error):
        return []
    return [db_path] if exists else []


def _opencode_binary() -> str | None:
    """Resolve the ``opencode`` executable (``AGT_OPENCODE_BIN`` wins)."""
    override = os.environ.get(_OPENCODE_BIN_ENV, "").strip()
    if override:
        return override
    return shutil.which("opencode")


def delete_opencode_session(session_id: str) -> ProviderSessionDeleteResult:
    """Delete one OpenCode session through the ``opencode`` CLI.

    AGT never writes the SQLite store itself. ``opencode session delete``
    owns the write path: it also removes child sessions and lets dependent
    tables cascade. ``--standalone`` runs a private server against the same
    data directory instead of attaching to a shared background service
    that may be stale or belong to another OpenCode version.

    Existence is probed read-only first so a missing session keeps the same
    ``missing=True`` result as the file-backed providers.
    """
    _validate_session_id(session_id)
    if session_id.startswith("-"):
        # Never let a provider id be parsed as an option by the CLI.
        raise ValueError("invalid provider session id")
    db_path = _opencode_db_path()
    if not db_path.is_file():
        return ProviderSessionDeleteResult(supported=True, missing=True)
    try:
        exists: bool | None = _opencode_session_exists(db_path, session_id)
    except (OSError, sqlite3.Error):
        # A locked/corrupt store is inconclusive; let the CLI report it.
        exists = None
    if exists is False:
        return ProviderSessionDeleteResult(supported=True, missing=True)

    binary = _opencode_binary()
    if binary is None:
        return ProviderSessionDeleteResult(
            supported=True,
            errors=(
                "opencode CLI not found; add it to PATH or set "
                f"{_OPENCODE_BIN_ENV}",
            ),
        )
    try:
        completed = subprocess.run(
            [binary, "--standalone", "session", "delete", session_id],
            capture_output=True,
            text=True,
            timeout=_OPENCODE_DELETE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ProviderSessionDeleteResult(
            supported=True,
            errors=(
                "opencode session delete timed out after "
                f"{_OPENCODE_DELETE_TIMEOUT_SECONDS:.0f}s",
            ),
        )
    except OSError as exc:
        return ProviderSessionDeleteResult(
            supported=True,
            errors=(f"cannot run {binary}: {exc}",),
        )
    if completed.returncode == 0:
        return ProviderSessionDeleteResult(supported=True, missing=False)

    detail = (completed.stderr or completed.stdout or "").strip()
    if len(detail) > 400:
        detail = detail[-400:]
    if "not found" in detail.lower():
        return ProviderSessionDeleteResult(supported=True, missing=True)
    message = f"opencode session delete failed (exit {completed.returncode})"
    if detail:
        message = f"{message}: {detail}"
    return ProviderSessionDeleteResult(supported=True, errors=(message,))


def load_history(provider: str, session_id: str) -> list[Message]:
    """Provider-dispatched history loader."""
    if provider == "claude":
        return load_claude_history(session_id)
    if provider == "codex":
        return load_codex_history(session_id)
    if provider == "opencode":
        return load_opencode_history(session_id)
    raise NotImplementedError(
        f"history reader for provider '{provider}' is not yet implemented"
    )


def find_claude_session_files(session_id: str) -> list[Path]:
    """Locate Claude Code transcript files for an exact session id."""
    _validate_session_id(session_id)
    projects_root = Path.home() / ".claude" / "projects"
    if not projects_root.is_dir():
        return []
    matches: list[Path] = []
    for project_dir in projects_root.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            matches.append(candidate)
    return matches


def find_codex_session_files(session_id: str) -> list[Path]:
    """Locate Codex rollout files for an exact session uuid."""
    _validate_session_id(session_id)
    home = Path.home()
    roots = (home / ".codex" / "sessions", home / ".codex" / "archived_sessions")
    matches: list[Path] = []
    suffix = f"-{session_id}"
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.jsonl"):
            if path.stem == session_id or path.stem.endswith(suffix):
                matches.append(path)
    matches.sort(key=_mtime, reverse=True)
    return matches


def find_provider_session_files(provider: str, session_id: str) -> list[Path]:
    """Locate provider-local transcript files for one session id.

    For OpenCode the result is the shared SQLite database (OpenCode has no
    per-session file); treat it as an existence marker, never as a file to
    unlink.
    """
    if provider == "claude":
        return find_claude_session_files(session_id)
    if provider == "codex":
        return find_codex_session_files(session_id)
    if provider == "opencode":
        return find_opencode_session_files(session_id)
    raise NotImplementedError(
        f"history reader for provider '{provider}' is not yet implemented"
    )


def delete_provider_session(provider: str, session_id: str) -> ProviderSessionDeleteResult:
    """Permanently delete provider-local transcript files for one session."""
    # OpenCode sessions live inside a shared database, so the generic
    # "unlink every found path" path below must never see them.
    if provider == "opencode":
        return delete_opencode_session(session_id)
    try:
        paths = find_provider_session_files(provider, session_id)
    except NotImplementedError as exc:
        return ProviderSessionDeleteResult(
            supported=False,
            reason=str(exc),
        )

    deleted: list[Path] = []
    errors: list[str] = []
    for path in paths:
        try:
            path.unlink()
            deleted.append(path)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return ProviderSessionDeleteResult(
        supported=True,
        deleted_paths=tuple(deleted),
        missing=not paths,
        errors=tuple(errors),
    )


__all__ = [
    "Message",
    "load_history",
    "load_claude_history",
    "load_codex_history",
    "load_opencode_history",
    "ProviderSessionDeleteResult",
    "find_provider_session_files",
    "find_opencode_session_files",
    "delete_provider_session",
    "delete_opencode_session",
]
