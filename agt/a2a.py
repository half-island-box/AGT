"""A2A-aligned internal object model (Phase 2 artifact foundation).

This module defines the shared Message/Part vocabulary used by both the
server and the daemon. It deliberately mirrors the A2A protocol's object
model (Message / TextPart / DataPart / FilePart / artifact references)
**without** adopting the A2A wire protocol — transport remains the
existing HTTP + reverse-dial WebSocket links. Aligning names now means a
future external A2A endpoint is a channel swap, not a rewrite.

Concept mapping (see docs/impl/agent-orchestration-design.md §8.6):

- TextPart  → briefing (goal / context / pitfalls), free text
- DataPart  → structured contract (constraints / inputs / return)
- FilePart  → resource reference (URI only, never inline bytes)
- Message   → one Step's structured input
- contextId → SessionModel ``(job_id, identity_did, provider)``

Public artifact URIs are ALWAYS ``agt-artifact://<artifact_id>``. Daemon
local paths are an internal zero-copy detail and never appear in public
input/output payloads.
"""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

__all__ = [
    "TextPart",
    "DataPart",
    "FilePart",
    "Part",
    "Message",
    "message_from_prompt",
    "render_message",
    "make_artifact_uri",
    "parse_artifact_uri",
    "INLINE_MAX_BYTES",
    "REPORT_INLINE_BUDGET_BYTES",
    "MAX_ARTIFACT_FILE_BYTES",
    "MAX_ARTIFACTS_PER_STEP",
    "ARTIFACT_URI_PREFIX",
]

# ── Budgets (design D7; enforced on both daemon and server) ─────────
INLINE_MAX_BYTES = 4 * 1024
REPORT_INLINE_BUDGET_BYTES = 64 * 1024
MAX_ARTIFACT_FILE_BYTES = 256 * 1024 * 1024
MAX_ARTIFACTS_PER_STEP = 50

ARTIFACT_URI_PREFIX = "agt-artifact://"
_ARTIFACT_URI_RE = re.compile(
    r"^agt-artifact://([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)


class TextPart(BaseModel):
    """Free-text briefing: goal, context, constraints-in-prose, pitfalls."""

    type: Literal["text"] = "text"
    text: str


class DataPart(BaseModel):
    """Structured contract: constraints / inputs / return shape."""

    type: Literal["data"] = "data"
    data: dict


class FilePart(BaseModel):
    """Resource reference. URI only — heavy content stays out-of-band."""

    type: Literal["file"] = "file"
    uri: str
    mime_type: str | None = None


Part = Annotated[Union[TextPart, DataPart, FilePart], Field(discriminator="type")]


class Message(BaseModel):
    """One Step's structured input (A2A Message shape)."""

    role: str = "user"
    parts: list[Part] = Field(default_factory=list)


def message_from_prompt(prompt: str, *, role: str = "user") -> Message:
    """Normalize a legacy plain prompt into a single-TextPart Message."""
    return Message(role=role, parts=[TextPart(text=prompt)])


def make_artifact_uri(artifact_id: str) -> str:
    """Build the public reference for an artifact id."""
    return f"{ARTIFACT_URI_PREFIX}{artifact_id}"


def parse_artifact_uri(uri: str) -> str | None:
    """Extract the artifact id from an ``agt-artifact://`` URI.

    Returns ``None`` for anything else (file:// paths are internal and
    never valid public references).
    """
    m = _ARTIFACT_URI_RE.match(uri)
    return m.group(1) if m else None


def render_message(message: Message) -> str:
    """Render a structured Message into prompt text for a CLI agent.

    Rendering rules (spec: task-execution / 结构化消息输入渲染):
    - TextPart  → verbatim text
    - DataPart  → ``Structured input (JSON):`` + pretty JSON
    - FilePart  → ``Reference: <uri>`` (+ mime when present)

    A normalized prompt-only Message (single TextPart) renders to exactly
    the original prompt text — no decorators, no suffix.
    """
    blocks: list[str] = []
    for part in message.parts:
        if isinstance(part, TextPart):
            blocks.append(part.text)
        elif isinstance(part, DataPart):
            rendered = json.dumps(part.data, indent=2, ensure_ascii=False, sort_keys=True)
            blocks.append(f"Structured input (JSON):\n{rendered}")
        elif isinstance(part, FilePart):
            line = f"Reference: {part.uri}"
            if part.mime_type:
                line += f" ({part.mime_type})"
            blocks.append(line)
        else:  # pragma: no cover - discriminated union is closed
            continue
    return "\n\n".join(blocks)
