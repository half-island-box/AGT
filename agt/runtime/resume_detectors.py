"""ResumeRejectionDetector — judge whether a terminal event signals resume failure.

Phase 5: provider adapter layer. Each provider registers a detector that
inspects the structured ``result`` event from its stream sink and decides
whether the agent refused to resume an existing session.

The detector protocol is intentionally narrow — only the four fields
needed to disambiguate "resume was rejected" from other errors. Providers
whose stream format does not yet expose those fields register the
``GenericResumeRejectionDetector`` (always returns False) so the system
stays conservative: when in doubt, leave the existing session pointer in
place rather than wiping it on a heuristic guess.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ResumeRejectionDetector(ABC):
    """Decide whether a terminal event indicates resume rejection."""

    @abstractmethod
    def is_resume_rejection(
        self,
        *,
        is_error: bool,
        raw_error: dict[str, Any] | None,
        raw_event: dict[str, Any] | None,
    ) -> bool: ...


class ClaudeResumeRejectionDetector(ResumeRejectionDetector):
    """Detect resume rejection in Claude Code ``--output-format stream-json``.

    Observed signal (Claude Code 2.1.241, ``--resume <bad-uuid>``):
    the ``result`` event has ``is_error=true``,
    ``subtype="error_during_execution"``, and a string in ``errors`` that
    mentions "resume" or "session".
    """

    _RESUME_HINTS = ("resume", "session")

    def is_resume_rejection(
        self,
        *,
        is_error: bool,
        raw_error: dict[str, Any] | None,
        raw_event: dict[str, Any] | None,
    ) -> bool:
        if not is_error or not isinstance(raw_event, dict):
            return False
        subtype = str(raw_event.get("subtype") or "")
        errors_field = raw_event.get("errors") or []
        err_blob = " ".join(str(e) for e in errors_field if e) if isinstance(errors_field, list) else ""
        if isinstance(raw_error, dict):
            err_blob = " ".join(
                (
                    err_blob,
                    str(raw_error.get("type") or ""),
                    str(raw_error.get("message") or ""),
                )
            ).strip()
        lowered = err_blob.lower()
        if subtype == "error_during_execution" and any(h in lowered for h in self._RESUME_HINTS):
            return True
        return False


class GenericResumeRejectionDetector(ResumeRejectionDetector):
    """Conservative default for providers without a known signal shape.

    Always returns False — when in doubt, keep the existing session
    pointer. Providers whose stream format does carry a rejection signal
    MUST register their own detector rather than rely on this.
    """

    def is_resume_rejection(
        self,
        *,
        is_error: bool,
        raw_error: dict[str, Any] | None,
        raw_event: dict[str, Any] | None,
    ) -> bool:
        return False


__all__ = [
    "ResumeRejectionDetector",
    "ClaudeResumeRejectionDetector",
    "GenericResumeRejectionDetector",
]