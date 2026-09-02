"""Daemon-side structured-message rendering (Phase 2 input consumption).

Turns a TaskPayload's ``message`` dict into the prompt text a CLI agent
executes. Rendering rules (spec: task-execution / 结构化消息输入渲染):

- TextPart  → verbatim text
- DataPart  → ``Structured input (JSON):`` + pretty JSON
- FilePart  → an ``Input file:`` line pointing at a REAL readable path:
  server-inlined content (``inline_b64``) is materialized under
  ``<agent_cwd>/.agt/inputs/``; a same-daemon ``local_path`` hint is
  referenced zero-copy; anything else goes through the caller's
  ``resolver`` (``artifacts.get`` RPC / HTTP).

Unresolvable references raise — an incomplete input must fail loudly,
never silently degrade. Legacy prompt-only tasks never enter this module.
"""

from __future__ import annotations

import base64
import json
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

from ..a2a import parse_artifact_uri

Resolver = Callable[[str], Awaitable[dict[str, Any] | None]]

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_MIME_EXTS = {
    "text/markdown": ".md",
    "text/plain": ".txt",
    "application/json": ".json",
    "text/x-diff": ".diff",
    "application/patch": ".patch",
}


def _cache_name(uri: str, mime_type: str | None = None) -> str:
    artifact_id = parse_artifact_uri(uri) or "input"
    stem = _SAFE_NAME_RE.sub("_", artifact_id)
    ext = ""
    if mime_type:
        ext = _MIME_EXTS.get(mime_type, "")
        if not ext:
            _, _, sub = mime_type.partition("/")
            if sub and sub != "octet-stream":
                ext = f".{sub}"
    return f"{stem}{ext}"


async def render_task_message(
    message: dict,
    *,
    agent_cwd: Path,
    resolver: Resolver | None = None,
) -> str:
    """Render a structured message into agent prompt text."""
    inputs_dir = agent_cwd / ".agt" / "inputs"
    blocks: list[str] = []

    for part in message.get("parts", []):
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            blocks.append(str(part.get("text", "")))
        elif ptype == "data":
            data = part.get("data") or {}
            rendered = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
            blocks.append(f"Structured input (JSON):\n{rendered}")
        elif ptype == "file":
            uri = str(part.get("uri", ""))
            mime = part.get("mime_type") or None
            path = await _resolve_file_part(
                part, uri=uri, mime=mime, inputs_dir=inputs_dir, resolver=resolver
            )
            line = f"Input file: {path}"
            if mime:
                line += f" ({mime})"
            blocks.append(line)

    return "\n\n".join(blocks)


async def _resolve_file_part(
    part: dict,
    *,
    uri: str,
    mime: str | None,
    inputs_dir: Path,
    resolver: Resolver | None,
) -> Path:
    """Resolve one FilePart to a locally readable path."""
    # 1. Server-inlined small content → materialize under .agt/inputs/.
    inline_b64 = part.get("inline_b64")
    if isinstance(inline_b64, str) and inline_b64:
        data = base64.b64decode(inline_b64)
        inputs_dir.mkdir(parents=True, exist_ok=True)
        target = inputs_dir / _cache_name(uri, mime)
        target.write_bytes(data)
        return target

    # 2. Same-daemon zero-copy hint from the claim payload.
    local_path = part.get("local_path")
    if isinstance(local_path, str) and local_path:
        candidate = Path(local_path)
        if candidate.exists():
            return candidate

    # 3. On-demand resolution (artifacts.get RPC / HTTP content).
    artifact_id = parse_artifact_uri(uri)
    if artifact_id and resolver is not None:
        resolved = await resolver(artifact_id)
        if isinstance(resolved, dict):
            remote_path = resolved.get("local_path")
            if isinstance(remote_path, str) and Path(remote_path).exists():
                return Path(remote_path)
            content_b64 = resolved.get("content_b64")
            if isinstance(content_b64, str) and content_b64:
                data = base64.b64decode(content_b64)
                inputs_dir.mkdir(parents=True, exist_ok=True)
                target = inputs_dir / _cache_name(uri, mime or resolved.get("mime_type"))
                target.write_bytes(data)
                return target

    raise ValueError(f"unresolved input reference: {uri}")


__all__ = ["render_task_message"]
