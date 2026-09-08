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


__all__ = [
    "SPAWN_PREFIX",
    "SPAWN_INSTRUCTION",
    "SpawnParseResult",
    "parse_spawn_markers",
    "mask_spawn_lines",
    "MAX_BATCH",
]
