"""AGT-SPAWN marker-line protocol (Phase 3 spawn intent).

A parent agent expresses delegation intents by emitting single-line
markers in its stdout::

    AGT-SPAWN {"identity":"tester-a","goal":"...","inputs":[],"return":"..."}

One line = one child envelope. This module owns the daemon-side half:
scan captured stdout, parse envelopes (best-effort — a malformed line is
skipped with a warning, never failing the Step report), and strip the
markers from user-visible output.

Budgets are enforced server-side (spawn 预算 spec); the daemon
pre-truncates to the same batch cap purely to bound the report payload.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from ..a2a import MAX_ARTIFACTS_PER_STEP

log = logging.getLogger(__name__)

SPAWN_PREFIX = "AGT-SPAWN "
SPAWN_LINE_RE = re.compile(r"^AGT-SPAWN\s+(\{.*\})\s*$")
MAX_BATCH = 8  # mirrors server MAX_SPAWN_BATCH (payload bounding only)

SPAWN_INSTRUCTION = (
    "[agt] To delegate parallel sub-tasks, output one line per child, "
    'exactly: AGT-SPAWN {"identity": "<did or empty>", "goal": "...", '
    '"inputs": ["agt-artifact://..."], "return": "...", '
    '"daemon_id": "<optional>", "provider": "<optional>"} — single-line '
    "JSON, no code fences. Omit daemon_id/provider to route dynamically; "
    "if you name one and it cannot run the requested provider, the child "
    "fails instead of being rerouted."
)

# ── AGT-ASK: explicit "ask the user" marker (goal mode G2b) ─────────
#
# task-type Jobs have no implicit needs_input path (that derivation is
# chat-only: clean exit ⇒ waiting_input). AGT-ASK is the explicit
# channel: one JSON line asking a question; the daemon reports
# outcome=needs_input with the question in outcome_payload. ASK wins
# over SPAWN when both appear in the same turn.

ASK_PREFIX = "AGT-ASK "
ASK_LINE_RE = re.compile(r"^AGT-ASK\s+(\{.*\})\s*$")

ASK_INSTRUCTION = (
    "[agt] If you are genuinely blocked and cannot proceed without the "
    'user\'s answer, output exactly one line: AGT-ASK {"question": "..."} '
    "— single-line JSON, no code fences. Use it ONLY when truly blocked; "
    "the task pauses until answered. Do not combine AGT-ASK with "
    "AGT-SPAWN in the same turn (the ask wins, spawn intents are dropped)."
)


@dataclass
class SpawnParseResult:
    """Outcome of scanning one Step's captured stdout."""

    cleaned_stdout: str
    children: list[dict]
    spawned_count: int = 0  # lines recognized (parsed or not)


def parse_spawn_markers(stdout: str) -> SpawnParseResult:
    """Extract spawn envelopes and strip marker lines from stdout.

    - Marker lines are removed entirely; when any spawn was recognized a
      one-line ``[agt] spawned N sub-task(s)`` summary is appended.
    - Malformed JSON on a marker line: line stripped, warning logged,
      envelope skipped.
    - Envelope shape is validated minimally (``goal`` required non-empty
      string; identity/inputs/return optional) — the server re-validates.
    """
    kept: list[str] = []
    children: list[dict] = []
    spawned = 0

    for line in stdout.splitlines():
        match = SPAWN_LINE_RE.match(line)
        if match is None:
            if line.startswith(SPAWN_PREFIX):
                spawned += 1
                log.warning("AGT-SPAWN line is not valid JSON; skipping: %.120s", line)
                continue
            kept.append(line)
            continue

        spawned += 1
        try:
            envelope = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning("AGT-SPAWN JSON decode failed; skipping line")
            continue
        if not isinstance(envelope, dict) or not isinstance(envelope.get("goal"), str) or not envelope.get("goal"):
            log.warning("AGT-SPAWN envelope missing goal; skipping")
            continue
        if len(children) >= MAX_BATCH:
            log.warning("AGT-SPAWN batch exceeds %d; dropping the rest", MAX_BATCH)
            continue
        children.append(envelope)

    cleaned = "\n".join(kept)
    if stdout.endswith("\n") and cleaned:
        cleaned += "\n"
    if spawned:
        summary = f"[agt] spawned {len(children)} sub-task(s)"
        base = cleaned.rstrip("\n")
        cleaned = (base + "\n" + summary + "\n") if base else (summary + "\n")
    return SpawnParseResult(cleaned_stdout=cleaned, children=children, spawned_count=spawned)


def mask_spawn_lines(text: str) -> str:
    """Length-preserving mask for streaming progress prefixes.

    Progress frames are cumulative-prefix bookkept by length on the
    server (shrink protection), so marker lines cannot simply be removed
    mid-stream. Mask each ``AGT-SPAWN`` line with same-length filler —
    the envelope JSON never leaks, and the prefix stays monotonic.
    """
    if SPAWN_PREFIX not in text:
        return text
    return "\n".join(
        "*" * len(line) if line.startswith(SPAWN_PREFIX) else line
        for line in text.splitlines()
    )


@dataclass
class AskParseResult:
    """Outcome of scanning one Step's stdout for AGT-ASK lines."""

    cleaned_stdout: str
    asked: bool = False
    question: str | None = None


def parse_ask_markers(stdout: str) -> AskParseResult:
    """Extract the AGT-ASK question and strip marker lines from stdout.

    The first well-formed marker wins (one question per turn); malformed
    AGT-ASK lines are stripped with a warning. When asked, a
    ``[agt] waiting for input: …`` summary line is appended so the
    question stays visible in the reported stdout.
    """
    kept: list[str] = []
    asked = False
    question: str | None = None

    for line in stdout.splitlines():
        match = ASK_LINE_RE.match(line)
        if match is None:
            if line.startswith(ASK_PREFIX):
                log.warning("AGT-ASK line is not valid JSON; skipping: %.120s", line)
                continue
            kept.append(line)
            continue
        if asked:
            log.warning("multiple AGT-ASK lines; keeping only the first question")
            continue
        try:
            envelope = json.loads(match.group(1))
        except json.JSONDecodeError:
            log.warning("AGT-ASK JSON decode failed; skipping line")
            continue
        q = envelope.get("question") if isinstance(envelope, dict) else None
        if not isinstance(q, str) or not q.strip():
            log.warning("AGT-ASK envelope missing question; skipping")
            continue
        asked = True
        question = q.strip()

    cleaned = "\n".join(kept)
    if stdout.endswith("\n") and cleaned:
        cleaned += "\n"
    if asked:
        summary = f"[agt] waiting for input: {question}"
        base = cleaned.rstrip("\n")
        cleaned = (base + "\n" + summary + "\n") if base else (summary + "\n")
    return AskParseResult(cleaned_stdout=cleaned, asked=asked, question=question)


def derive_marker_outcome(stdout: str) -> tuple[str, str | None, dict | None]:
    """Terminal-outcome decision over a Step's raw captured stdout.

    Shared by the executor's report path (goal mode G2b). Returns
    ``(cleaned_stdout, outcome, outcome_payload)``:

    - AGT-ASK present → ``needs_input`` + ``{"ask": {"question": …}}``;
      ASK wins over SPAWN — spawn envelopes are dropped with a warning
      (the agent re-emits them after the answer arrives next turn).
    - AGT-SPAWN envelopes present → ``spawn_subjob`` + children.
    - neither → ``(stdout, None, None)`` (caller derives as before).
    """
    spawn = parse_spawn_markers(stdout)
    cleaned = spawn.cleaned_stdout if spawn.spawned_count else stdout
    ask = parse_ask_markers(cleaned)
    if ask.asked and ask.question is not None:
        if spawn.spawned_count:
            log.warning(
                "AGT-ASK and AGT-SPAWN in the same step; "
                "ASK wins, spawn envelopes dropped"
            )
        return (
            ask.cleaned_stdout,
            "needs_input",
            {"ask": {"question": ask.question}},
        )
    if spawn.spawned_count and spawn.children:
        return cleaned, "spawn_subjob", {"children": spawn.children}
    return cleaned, None, None


__all__ = [
    "SPAWN_PREFIX",
    "SPAWN_INSTRUCTION",
    "ASK_PREFIX",
    "ASK_INSTRUCTION",
    "SpawnParseResult",
    "parse_spawn_markers",
    "AskParseResult",
    "parse_ask_markers",
    "derive_marker_outcome",
    "mask_spawn_lines",
    "MAX_BATCH",
]
