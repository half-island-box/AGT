"""Tests for ``ResumeRejectionDetector`` implementations.

Phase 5: detectors inspect structured terminal events (not stderr text).
The Claude Code detector must recognise the observed
``error_during_execution`` shape; the generic detector must always be
conservative (return False) so unknown providers don't wipe their
session pointer on a guess.
"""

from __future__ import annotations

from agt.runtime.resume_detectors import (
    ClaudeResumeRejectionDetector,
    GenericResumeRejectionDetector,
)


def _claude_event(subtype, errors):
    return {"subtype": subtype, "errors": errors}


def test_claude_detector_accepts_successful_result():
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=False,
        raw_error=None,
        raw_event=_claude_event("success", []),
    ) is False


def test_claude_detector_rejects_non_resume_error():
    """An error that does NOT mention resume / session is not a resume rejection."""
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=True,
        raw_error=None,
        raw_event=_claude_event("error_during_execution", ["out of memory"]),
    ) is False


def test_claude_detector_recognises_resume_error_subtype_and_message():
    """``error_during_execution`` + a string mentioning ``resume`` → True."""
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=True,
        raw_error=None,
        raw_event=_claude_event(
            "error_during_execution",
            ["--resume requires a valid session ID"],
        ),
    ) is True


def test_claude_detector_recognises_session_error_subtype_and_message():
    """``error_during_execution`` + a string mentioning ``session`` → True."""
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=True,
        raw_error=None,
        raw_event=_claude_event(
            "error_during_execution",
            ["Session not found in projects cache"],
        ),
    ) is True


def test_claude_detector_requires_error_subtype():
    """Even with ``resume`` in the message, missing ``error_during_execution`` is not enough."""
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=True,
        raw_error=None,
        raw_event=_claude_event("success", ["resume ok"]),
    ) is False


def test_claude_detector_handles_non_dict_event():
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(is_error=True, raw_error=None, raw_event=None) is False


def test_generic_detector_always_returns_false():
    """The generic detector is a safe default for unknown providers."""
    det = GenericResumeRejectionDetector()
    assert det.is_resume_rejection(is_error=True, raw_error=None, raw_event={"subtype": "anything"}) is False
    assert det.is_resume_rejection(is_error=False, raw_error=None, raw_event=None) is False


def test_claude_detector_inspects_raw_error_fallback():
    """When the event lacks ``errors``, fall back to ``raw_error.type/message``."""
    det = ClaudeResumeRejectionDetector()
    assert det.is_resume_rejection(
        is_error=True,
        raw_error={"type": "resume_not_found", "message": "nope"},
        raw_event={"subtype": "error_during_execution"},
    ) is True