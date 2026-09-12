"""Tests for the provider adapter registry.

Phase 5: ``get_adapter`` is the single entry point the executor uses to
look up provider-specific collaborators. These tests pin the registry
contract: every built-in provider has an adapter, unknown providers
fall back to a sensible default, and the four collaborator fields are
always populated.
"""

from __future__ import annotations

from agt.runtime.registry import (
    ProviderAdapter,
    get_adapter,
    registered_providers,
)


def test_every_known_provider_has_an_adapter():
    """Every provider in ``KNOWN_AGENTS`` resolves to a concrete adapter."""
    from agt.probe.known_agents import KNOWN_AGENTS

    for agent in KNOWN_AGENTS:
        adapter = get_adapter(agent.provider)
        assert isinstance(adapter, ProviderAdapter), f"{agent.provider} missing adapter"
        assert adapter.command_builder is not None
        assert adapter.stream_sink_factory is not None
        assert adapter.resume_rejection_detector is not None
        # session_locator is allowed to be None (no disk fallback).


def test_registered_providers_includes_all_known():
    """The registry covers every KnownAgent provider."""
    from agt.probe.known_agents import KNOWN_AGENTS

    known = {a.provider for a in KNOWN_AGENTS}
    assert set(registered_providers()) >= known


def test_unknown_provider_falls_back_to_claude_style():
    """Unknown providers get a Claude-shaped adapter (no streaming, no disk fallback)."""
    adapter = get_adapter("not-a-real-provider-xyz")
    assert isinstance(adapter, ProviderAdapter)
    # Falls back to Claude-shaped command builder.
    from agt.runtime.command_builders import ClaudeCommandBuilder

    assert isinstance(adapter.command_builder, ClaudeCommandBuilder)
    # No streaming: factory returns NoopSink.
    from agt.runtime.streaming import NoopSink

    assert isinstance(adapter.stream_sink_factory(), NoopSink)


def test_claude_adapter_uses_stream_sink_factory():
    """Claude's factory returns ``ClaudeStreamSink`` instances."""
    from agt.runtime.streaming import ClaudeStreamSink

    adapter = get_adapter("claude")
    assert isinstance(adapter.stream_sink_factory(), ClaudeStreamSink)


def test_codex_adapter_uses_noop_sink_phase5():
    """Phase 5 leaves Codex on the legacy pipe-text path."""
    from agt.runtime.streaming import NoopSink

    adapter = get_adapter("codex")
    assert isinstance(adapter.stream_sink_factory(), NoopSink)


def test_unknown_provider_does_not_grant_streaming():
    """Unknown providers MUST NOT silently get streaming — only the explicit registry row grants it."""
    adapter = get_adapter("not-a-real-provider-xyz")
    from agt.runtime.streaming import NoopSink

    assert isinstance(adapter.stream_sink_factory(), NoopSink)


def test_claude_adapter_carries_bypass_permission_args():
    """Claude spawns with ``--permission-mode bypassPermissions`` so the
    headless ``-p`` process can write deliverables without a TTY prompt."""
    from agt.runtime.registry import CLAUDE_BYPASS_PERMISSION_ARGS

    adapter = get_adapter("claude")
    assert adapter.permission_args == CLAUDE_BYPASS_PERMISSION_ARGS
    assert adapter.permission_args == ("--permission-mode", "bypassPermissions")


def test_codex_adapter_carries_sandbox_args():
    """Codex spawns with ``--sandbox workspace-write`` so the non-interactive
    ``exec`` process can write deliverables (``.agt/outputs/``) instead of
    being stuck in its default read-only sandbox. ``--approval-mode full-auto``
    prevents the default ``suggest`` mode from prompting for approval on an
    invisible stderr stream."""
    from agt.runtime.registry import CODEX_SANDBOX_ARGS

    adapter = get_adapter("codex")
    assert adapter.permission_args == CODEX_SANDBOX_ARGS
    assert adapter.permission_args == (
        "--sandbox",
        "workspace-write",
        "--approval-mode",
        "full-auto",
    )


def test_non_claude_adapters_have_empty_permission_args():
    """Providers without a headless write-bypass need MUST NOT receive any
    permission/sandbox flags.

    cursor/copilot/qwen/... reuse ``ClaudeCommandBuilder`` for their ``-p``
    shape but are not Claude Code — injecting ``--permission-mode`` would
    make them fail to start.
    """
    for provider in ("cursor", "copilot", "qwen", "kimi", "opencode", "openclaw"):
        assert get_adapter(provider).permission_args == (), provider


def test_unknown_provider_has_empty_permission_args():
    """The fallback adapter must not inject Claude-only flags either."""
    assert get_adapter("not-a-real-provider-xyz").permission_args == ()