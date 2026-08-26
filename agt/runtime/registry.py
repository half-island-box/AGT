"""ProviderAdapter — central registry mapping provider name to its adapter bundle.

Phase 5: provider adapter layer. Each provider registers a
``ProviderAdapter`` containing the four provider-specific collaborators:

- ``command_builder`` — how to assemble the agent CLI argv
- ``session_locator`` — how to discover a fresh ``session_id`` on disk
- ``stream_sink_factory`` — how to consume the streaming event stream
  (returns ``NoopSink`` for providers whose stream format is not yet
  implemented; the executor falls back to the legacy pipe-text path)
- ``resume_rejection_detector`` — how to recognize a failed resume

The executor imports :func:`get_adapter` and never references provider
names as string literals. Adding a new provider means:

1. Implement the four collaborators (or reuse defaults via composition).
2. Register an entry in ``_REGISTRY``.
3. Add a ``KnownAgent`` row in :mod:`agt.probe.known_agents` so probe
   recognises the CLI on the user's machine.

No change to ``executor.py`` / ``cli/main.py`` / ``daemon/*`` is required.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .command_builders import (
    ClaudeCommandBuilder,
    CodexCommandBuilder,
    CommandBuilder,
)
from .resume_detectors import (
    ClaudeResumeRejectionDetector,
    GenericResumeRejectionDetector,
    ResumeRejectionDetector,
)
from .session_locators import (
    SessionLocator,
    claude_session_locator,
    codex_session_locator,
)
from .streaming import (
    ClaudeStreamSink,
    NoopSink,
    StreamingSink,
)


@dataclass(frozen=True)
class ProviderAdapter:
    """Bundle of provider-specific collaborators.

    Each field is a small protocol/concrete class so callers can compose
    or override individual pieces (e.g. point ``stream_sink_factory`` at
    a custom sink in tests).
    """

    command_builder: CommandBuilder
    session_locator: SessionLocator | None
    stream_sink_factory: Callable[[], StreamingSink]
    resume_rejection_detector: ResumeRejectionDetector


# Built-in adapters. New providers should be added here.
def _claude_adapter() -> ProviderAdapter:
    return ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=claude_session_locator(),
        stream_sink_factory=ClaudeStreamSink,
        resume_rejection_detector=ClaudeResumeRejectionDetector(),
    )


def _codex_adapter() -> ProviderAdapter:
    return ProviderAdapter(
        command_builder=CodexCommandBuilder(),
        session_locator=codex_session_locator(),
        # Phase 5 leaves Codex on the legacy pipe-text path; flip the
        # factory to CodexStreamSink in Phase B once that sink lands.
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    )


def _claude_default_adapter() -> ProviderAdapter:
    """Default for unknown / unconfigured providers.

    Falls back to the Claude command shape (most other provider CLIs are
    ``-p`` style), with no stream sink and no disk fallback. Keeps unknown
    providers functional without granting them features we have not
    validated against their actual CLI behaviour.
    """

    return ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    )


_REGISTRY: dict[str, ProviderAdapter] = {
    "claude": _claude_adapter(),
    "codex": _codex_adapter(),
    # cursor / copilot / qwen / kimi / opencode / openclaw share the
    # Claude ``-p`` shape; explicit registry rows document that intent
    # and let us flip individual fields per-provider later.
    "cursor": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
    "copilot": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
    "qwen": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
    "kimi": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
    "opencode": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
    "openclaw": ProviderAdapter(
        command_builder=ClaudeCommandBuilder(),
        session_locator=None,
        stream_sink_factory=NoopSink,
        resume_rejection_detector=GenericResumeRejectionDetector(),
    ),
}


def get_adapter(provider: str) -> ProviderAdapter:
    """Return the adapter for ``provider``; fall back to the Claude default.

    Unknown providers receive the same shape Claude uses (``-p`` style
    CLI, no streaming, no disk fallback). This keeps the system
    functional for newly-added CLIs that have not yet been wired in —
    but it does NOT grant them streaming or session-resume features
    until a registry row is added explicitly.
    """
    adapter = _REGISTRY.get(provider)
    if adapter is not None:
        return adapter
    return _claude_default_adapter()


def registered_providers() -> tuple[str, ...]:
    """Names of providers with explicit registry entries (for tests/CLI)."""
    return tuple(sorted(_REGISTRY.keys()))


__all__ = [
    "ProviderAdapter",
    "get_adapter",
    "registered_providers",
]