"""Tests for the CLI's provider auto-selection logic.

Exercises ``_pick_provider`` and the two candidate-listing helpers
(``_local_candidate_providers`` / ``_remote_candidate_providers``) in
isolation. The ``DaemonController`` / ``ServerClient`` calls are patched
so the tests don't touch the real filesystem or network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agt.cli.main import (
    _local_candidate_providers,
    _pick_provider,
    _remote_candidate_providers,
)


# ── helpers ───────────────────────────────────────────────────────


def _entry(provider: str, available: bool = True) -> MagicMock:
    """Build a MagicMock that quacks like an AgentEntry."""
    e = MagicMock()
    e.provider = provider
    e.is_available.return_value = available
    return e


def _runtime(daemon_id: str, provider: str, status: str = "online") -> dict:
    return {
        "daemon_id": daemon_id,
        "type": provider,
        "status": status,
        "name": f"{provider} on {daemon_id}",
        "version": "1.0",
        "device_name": "test-device",
    }


# ── _local_candidate_providers ────────────────────────────────────


def test_local_candidates_returns_probe_order():
    entries = [_entry("claude"), _entry("codex"), _entry("cursor")]
    with patch("agt.cli.main.DaemonController") as DC:
        DC.return_value.probe_agents = AsyncMock(return_value=entries)
        result = _local_candidate_providers()
    assert result == ["claude", "codex", "cursor"]


def test_local_candidates_filters_unavailable():
    entries = [
        _entry("claude", available=True),
        _entry("codex", available=False),
        _entry("cursor", available=True),
    ]
    with patch("agt.cli.main.DaemonController") as DC:
        DC.return_value.probe_agents = AsyncMock(return_value=entries)
        result = _local_candidate_providers()
    assert result == ["claude", "cursor"]


def test_local_candidates_empty():
    with patch("agt.cli.main.DaemonController") as DC:
        DC.return_value.probe_agents = AsyncMock(return_value=[])
        result = _local_candidate_providers()
    assert result == []


def test_local_candidates_reuses_controller():
    """Passing a controller avoids constructing a new DaemonController."""
    mock_controller = MagicMock()
    mock_controller.probe_agents = AsyncMock(
        return_value=[_entry("claude"), _entry("codex")]
    )
    with patch("agt.cli.main.DaemonController") as DC:
        result = _local_candidate_providers(controller=mock_controller)
    assert result == ["claude", "codex"]
    DC.assert_not_called()
    mock_controller.probe_agents.assert_awaited_once()


# ── _remote_candidate_providers ───────────────────────────────────


def _mock_server_client(runtimes: list[dict]) -> MagicMock:
    """Build a mock that quacks like an `async with ServerClient(...) as c`."""
    instance = MagicMock()
    instance.list_runtimes = AsyncMock(return_value=runtimes)
    instance.__aenter__ = AsyncMock(return_value=instance)
    instance.__aexit__ = AsyncMock(return_value=None)
    return instance


def test_remote_candidates_filters_by_daemon_id():
    runtimes = [
        _runtime("daemon-A", "claude"),
        _runtime("daemon-B", "codex"),
        _runtime("daemon-A", "cursor"),
    ]
    with patch("agt.cli.main.ServerClient") as SC:
        SC.return_value = _mock_server_client(runtimes)
        result = _remote_candidate_providers("daemon-A", "http://server")
    assert result == ["claude", "cursor"]


def test_remote_candidates_filters_offline():
    runtimes = [
        _runtime("daemon-A", "claude", status="online"),
        _runtime("daemon-A", "codex", status="offline"),
    ]
    with patch("agt.cli.main.ServerClient") as SC:
        SC.return_value = _mock_server_client(runtimes)
        result = _remote_candidate_providers("daemon-A", "http://server")
    assert result == ["claude"]


def test_remote_candidates_preserves_server_order():
    """Server returns rows ordered by (daemon_id, provider); we keep that order."""
    runtimes = [
        _runtime("daemon-A", "zulu"),
        _runtime("daemon-A", "alpha"),
        _runtime("daemon-A", "mike"),
    ]
    with patch("agt.cli.main.ServerClient") as SC:
        SC.return_value = _mock_server_client(runtimes)
        result = _remote_candidate_providers("daemon-A", "http://server")
    assert result == ["zulu", "alpha", "mike"]


def test_remote_candidates_empty():
    with patch("agt.cli.main.ServerClient") as SC:
        SC.return_value = _mock_server_client([])
        result = _remote_candidate_providers("daemon-A", "http://server")
    assert result == []


# ── _pick_provider: local (no --daemon) ───────────────────────────


def test_pick_local_auto_picks_first():
    with patch(
        "agt.cli.main._local_candidate_providers",
        return_value=["claude", "codex"],
    ):
        assert _pick_provider(None, None, "http://server") == "claude"


def test_pick_local_explicit_valid():
    with patch(
        "agt.cli.main._local_candidate_providers",
        return_value=["claude", "codex"],
    ):
        assert _pick_provider("codex", None, "http://server") == "codex"


def test_pick_local_explicit_invalid_exits_2(capsys):
    with patch(
        "agt.cli.main._local_candidate_providers",
        return_value=["claude"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            _pick_provider("codex", None, "http://server")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "codex" in err
    assert "claude" in err  # listed as available
    assert "locally" in err


def test_pick_local_no_candidates_exits_2(capsys):
    with patch(
        "agt.cli.main._local_candidate_providers",
        return_value=[],
    ):
        with pytest.raises(SystemExit) as exc_info:
            _pick_provider(None, None, "http://server")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "no local agents" in err.lower()


def test_pick_local_reuses_controller():
    """When local_controller is given, no new DaemonController is constructed."""
    mock_controller = MagicMock()
    with patch(
        "agt.cli.main._local_candidate_providers",
        return_value=["claude"],
    ) as m:
        result = _pick_provider(
            None, None, "http://server", local_controller=mock_controller,
        )
    assert result == "claude"
    m.assert_called_once_with(mock_controller)


# ── _pick_provider: remote (with --daemon) ────────────────────────


def test_pick_remote_auto_picks_first():
    with patch(
        "agt.cli.main._remote_candidate_providers",
        return_value=["cursor", "claude"],
    ) as m:
        assert _pick_provider(None, "daemon-A", "http://server") == "cursor"
    m.assert_called_once_with("daemon-A", "http://server")


def test_pick_remote_explicit_valid():
    with patch(
        "agt.cli.main._remote_candidate_providers",
        return_value=["claude", "codex"],
    ):
        assert _pick_provider("codex", "daemon-A", "http://server") == "codex"


def test_pick_remote_explicit_invalid_exits_2(capsys):
    with patch(
        "agt.cli.main._remote_candidate_providers",
        return_value=["claude"],
    ):
        with pytest.raises(SystemExit) as exc_info:
            _pick_provider("codex", "daemon-A", "http://server")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "codex" in err
    assert "claude" in err
    assert "daemon-A" in err


def test_pick_remote_no_candidates_exits_2(capsys):
    with patch(
        "agt.cli.main._remote_candidate_providers",
        return_value=[],
    ):
        with pytest.raises(SystemExit) as exc_info:
            _pick_provider(None, "daemon-A", "http://server")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "daemon-A" in err
    assert "no online runtimes" in err.lower()
