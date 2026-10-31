"""Tests for daemon persistent configuration file support."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from agt.daemon.config import (
    DaemonConfig,
    DaemonConfigError,
    ManagedDaemonConfig,
    daemon_config_file_path,
    load_daemon_config_file,
    remove_daemon_config_keys,
    resolve_daemon_settings,
    update_daemon_config_file,
)
from agt.cli.main import cli


def test_config_file_path_uses_home():
    assert daemon_config_file_path() == Path.home() / ".agt" / "daemon.json"


def test_load_missing_file_returns_none(tmp_path):
    missing = tmp_path / "daemon.json"
    assert load_daemon_config_file(missing) is None


def test_load_valid_file(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "max_concurrent": 4,
                "agent_timeout": 1800,
                "work_dir": str(tmp_path / "work"),
                "agent_cwd_allowlist": [str(tmp_path / "a"), str(tmp_path / "b")],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_daemon_config_file(path)
    assert isinstance(cfg, ManagedDaemonConfig)
    assert cfg.max_concurrent == 4
    assert cfg.agent_timeout == 1800
    assert cfg.work_dir == tmp_path / "work"
    assert cfg.agent_cwd_allowlist == [tmp_path / "a", tmp_path / "b"]


def test_load_malformed_json_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(DaemonConfigError, match="invalid JSON"):
        load_daemon_config_file(path)


def test_load_unknown_keys_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps({"version": 1, "max_concurrent": 2, "unknown_key": 1}),
        encoding="utf-8",
    )
    with pytest.raises(DaemonConfigError, match="unknown configuration keys"):
        load_daemon_config_file(path)


def test_load_unsupported_version_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    with pytest.raises(DaemonConfigError, match="unsupported configuration version"):
        load_daemon_config_file(path)


def test_load_missing_version_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(json.dumps({"max_concurrent": 2}), encoding="utf-8")
    with pytest.raises(DaemonConfigError, match="unsupported configuration version"):
        load_daemon_config_file(path)


@pytest.mark.parametrize("key", ["max_concurrent", "agent_timeout"])
def test_load_non_integer_timeout_or_concurrency_raises(tmp_path, key):
    path = tmp_path / "daemon.json"
    data = {"version": 1, key: "four"}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DaemonConfigError, match=f"{key} must be a positive integer"):
        load_daemon_config_file(path)


@pytest.mark.parametrize("key", ["max_concurrent", "agent_timeout"])
def test_load_zero_or_negative_integer_raises(tmp_path, key):
    path = tmp_path / "daemon.json"
    data = {"version": 1, key: 0}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(DaemonConfigError, match=f"{key} must be a positive integer"):
        load_daemon_config_file(path)


def test_load_relative_work_dir_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps({"version": 1, "work_dir": "relative/path"}),
        encoding="utf-8",
    )
    with pytest.raises(DaemonConfigError, match="path must be absolute"):
        load_daemon_config_file(path)


def test_load_empty_allowlist_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps({"version": 1, "agent_cwd_allowlist": []}),
        encoding="utf-8",
    )
    with pytest.raises(DaemonConfigError, match="non-empty array"):
        load_daemon_config_file(path)


def test_load_allowlist_with_relative_entry_raises(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {"version": 1, "agent_cwd_allowlist": [str(tmp_path / "ok"), "relative"]}
        ),
        encoding="utf-8",
    )
    with pytest.raises(DaemonConfigError, match="path must be absolute"):
        load_daemon_config_file(path)


def test_path_normalization_expands_tilde(tmp_path, monkeypatch):
    path = tmp_path / "daemon.json"
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "work_dir": "~/work/",
                "agent_cwd_allowlist": ["~/allow/"],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_daemon_config_file(path)
    assert cfg.work_dir == fake_home / "work"
    assert cfg.agent_cwd_allowlist == [fake_home / "allow"]


def test_allowlist_deduplication(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "agent_cwd_allowlist": [str(tmp_path), str(tmp_path) + "/"],
            }
        ),
        encoding="utf-8",
    )
    cfg = load_daemon_config_file(path)
    assert len(cfg.agent_cwd_allowlist) == 1


def test_resolve_defaults_when_no_file_and_no_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    settings = resolve_daemon_settings(
        file_cfg=None, environ={}
    )
    defaults = DaemonConfig()
    assert settings["max_concurrent"].value == defaults.max_concurrent_tasks
    assert settings["max_concurrent"].source == "default"
    assert settings["agent_timeout"].value == defaults.agent_timeout
    assert settings["agent_timeout"].source == "default"
    assert settings["work_dir"].value == defaults.work_dir_root
    assert settings["work_dir"].source == "default"
    assert settings["agent_cwd_allowlist"].value == (defaults.agent_cwd_allowlist or [Path.home()])
    assert settings["agent_cwd_allowlist"].source == "default"


def test_resolve_file_overrides_default(tmp_path):
    cfg = ManagedDaemonConfig(
        max_concurrent=4,
        agent_timeout=1800,
        work_dir=tmp_path / "work",
        agent_cwd_allowlist=[tmp_path / "allow"],
    )
    settings = resolve_daemon_settings(file_cfg=cfg, environ={})
    assert settings["max_concurrent"].value == 4
    assert settings["max_concurrent"].source == "file"
    assert settings["agent_timeout"].value == 1800
    assert settings["work_dir"].value == tmp_path / "work"
    assert settings["agent_cwd_allowlist"].value == [tmp_path / "allow"]


def test_resolve_env_overrides_file(tmp_path):
    cfg = ManagedDaemonConfig(max_concurrent=4, agent_timeout=1800)
    environ = {
        "AGT_MAX_CONCURRENT": "8",
        "AGT_AGENT_TIMEOUT": "3600",
        "AGT_WORK_DIR": str(tmp_path / "env_work"),
        "AGT_AGENT_CWD_ALLOWLIST": f"{tmp_path / 'a'}:{tmp_path / 'b'}",
    }
    settings = resolve_daemon_settings(file_cfg=cfg, environ=environ)
    assert settings["max_concurrent"].value == 8
    assert settings["max_concurrent"].source == "env"
    assert settings["max_concurrent"].file_value == 4
    assert settings["agent_timeout"].value == 3600
    assert settings["work_dir"].value == tmp_path / "env_work"
    assert settings["agent_cwd_allowlist"].value == [tmp_path / "a", tmp_path / "b"]


def test_resolve_invalid_env_integer_raises():
    with pytest.raises(DaemonConfigError, match="AGT_MAX_CONCURRENT"):
        resolve_daemon_settings(
            file_cfg=None, environ={"AGT_MAX_CONCURRENT": "four"}
        )


def test_from_env_uses_file_values(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    # Prevent from_env from failing on daemon_id / networking side effects.
    config_path = fake_home / ".agt" / "daemon.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"version": 1, "max_concurrent": 7, "agent_timeout": 900}),
        encoding="utf-8",
    )
    cfg = DaemonConfig.from_env()
    assert cfg.max_concurrent_tasks == 7
    assert cfg.agent_timeout == 900


def test_from_env_env_still_wins(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("AGT_MAX_CONCURRENT", "3")
    config_path = fake_home / ".agt" / "daemon.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"version": 1, "max_concurrent": 7}),
        encoding="utf-8",
    )
    cfg = DaemonConfig.from_env()
    assert cfg.max_concurrent_tasks == 3


def test_update_creates_file_with_owner_permissions(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file({"max_concurrent": 4}, path=path)
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == {"version": 1, "max_concurrent": 4}


def test_update_preserves_unrelated_keys(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file({"max_concurrent": 4, "agent_timeout": 1800}, path=path)
    update_daemon_config_file({"work_dir": str(tmp_path / "work")}, path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["max_concurrent"] == 4
    assert data["agent_timeout"] == 1800
    assert data["work_dir"] == str(tmp_path / "work")


def test_update_refuses_to_overwrite_malformed_file(tmp_path):
    path = tmp_path / "daemon.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(DaemonConfigError, match="malformed"):
        update_daemon_config_file({"max_concurrent": 4}, path=path)
    assert path.read_text(encoding="utf-8") == "not json"


def test_update_validates_before_writing(tmp_path):
    path = tmp_path / "daemon.json"
    with pytest.raises(DaemonConfigError, match="positive integer"):
        update_daemon_config_file({"max_concurrent": 0}, path=path)
    assert not path.exists()


def test_update_rejects_empty_allowlist(tmp_path):
    path = tmp_path / "daemon.json"
    with pytest.raises(DaemonConfigError, match="non-empty array"):
        update_daemon_config_file({"agent_cwd_allowlist": []}, path=path)


def test_update_normalizes_paths(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file(
        {"work_dir": str(tmp_path / "work/"), "agent_cwd_allowlist": [str(tmp_path / "allow/")]},
        path=path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["work_dir"] == str(tmp_path / "work")
    assert data["agent_cwd_allowlist"] == [str(tmp_path / "allow")]


def test_update_deduplicates_allowlist(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file(
        {"agent_cwd_allowlist": [str(tmp_path), str(tmp_path) + "/"]},
        path=path,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent_cwd_allowlist"] == [str(tmp_path)]


def test_remove_key(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file({"max_concurrent": 4, "agent_timeout": 1800}, path=path)
    remove_daemon_config_keys(["max_concurrent"], path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "max_concurrent" not in data
    assert data["agent_timeout"] == 1800


def test_remove_all_keys_deletes_file(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file({"max_concurrent": 4}, path=path)
    remove_daemon_config_keys(["max_concurrent"], path=path)
    assert not path.exists()


def test_remove_unknown_key_is_noop(tmp_path):
    path = tmp_path / "daemon.json"
    update_daemon_config_file({"max_concurrent": 4}, path=path)
    remove_daemon_config_keys(["agent_timeout"], path=path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["max_concurrent"] == 4


def test_remove_from_missing_file_is_noop(tmp_path):
    path = tmp_path / "daemon.json"
    remove_daemon_config_keys(["max_concurrent"], path=path)
    assert not path.exists()


# ── CLI tests ────────────────────────────────────────────────────────


def _cli_runner(tmp_path, monkeypatch):
    """Return a Click runner configured with an isolated HOME."""
    from click.testing import CliRunner

    monkeypatch.setenv("HOME", str(tmp_path))
    return CliRunner()


def test_cli_show_absent_file(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    result = runner.invoke(cli, ["daemon", "config", "show"])
    assert result.exit_code == 0
    assert "file does not exist" in result.output
    assert "source: default" in result.output


def test_cli_set_creates_file(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    work = tmp_path / "work"
    result = runner.invoke(
        cli,
        [
            "daemon",
            "config",
            "set",
            "--max-concurrent",
            "4",
            "--work-dir",
            str(work),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Saved configuration" in result.output
    cfg_path = tmp_path / ".agt" / "daemon.json"
    assert cfg_path.exists()
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert data["max_concurrent"] == 4
    assert data["work_dir"] == str(work)


def test_cli_set_rejects_invalid_value(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    result = runner.invoke(
        cli,
        ["daemon", "config", "set", "--max-concurrent", "0"],
    )
    assert result.exit_code == 2
    assert "positive integer" in result.output


def test_cli_set_rejects_relative_path(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    result = runner.invoke(
        cli,
        ["daemon", "config", "set", "--work-dir", "relative/path"],
    )
    assert result.exit_code == 2
    assert "absolute" in result.output


def test_cli_show_env_override_shows_saved_value(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    runner.invoke(cli, ["daemon", "config", "set", "--max-concurrent", "4"])
    result = runner.invoke(
        cli,
        ["daemon", "config", "show"],
        env={**os.environ, "HOME": str(tmp_path), "AGT_MAX_CONCURRENT": "8"},
    )
    assert result.exit_code == 0, result.output
    assert "source: env AGT_MAX_CONCURRENT" in result.output
    assert "saved: 4" in result.output
    assert "default: 1" in result.output


def test_cli_unset_removes_key(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    runner.invoke(cli, ["daemon", "config", "set", "--max-concurrent", "4"])
    result = runner.invoke(cli, ["daemon", "config", "unset", "--max-concurrent"])
    assert result.exit_code == 0, result.output
    assert "Removed settings" in result.output
    cfg_path = tmp_path / ".agt" / "daemon.json"
    assert not cfg_path.exists()


def test_cli_set_warns_about_root_allowlist(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    result = runner.invoke(
        cli,
        ["daemon", "config", "set", "--agent-cwd-allowlist", "/"],
    )
    assert result.exit_code == 0, result.output
    assert "Warning: '/' in agent_cwd_allowlist" in result.output


def test_cli_set_refuses_malformed_existing_file(tmp_path, monkeypatch):
    runner = _cli_runner(tmp_path, monkeypatch)
    cfg_path = tmp_path / ".agt" / "daemon.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json", encoding="utf-8")
    result = runner.invoke(
        cli,
        ["daemon", "config", "set", "--max-concurrent", "4"],
    )
    assert result.exit_code == 2
    assert "malformed" in result.output
    assert cfg_path.read_text(encoding="utf-8") == "not json"


# ── Daemon startup integration tests ─────────────────────────────────


def test_daemon_start_fails_clearly_with_invalid_config(tmp_path, monkeypatch, capsys):
    """cmd_start exits with a useful message when daemon.json is invalid."""
    from argparse import Namespace

    from agt.daemon import main as daemon_main

    monkeypatch.setenv("HOME", str(tmp_path))
    pid_file = tmp_path / ".agt" / "daemon.pid"
    monkeypatch.setattr(daemon_main, "PID_FILE", pid_file)

    cfg_path = tmp_path / ".agt" / "daemon.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text("not json", encoding="utf-8")

    result = daemon_main.cmd_start(Namespace(server_url=None))
    assert result == 2
    captured = capsys.readouterr()
    assert "Failed to load daemon configuration" in captured.err
    assert "Fix or remove" in captured.err


def test_daemon_start_uses_config_file_values(tmp_path, monkeypatch):
    """cmd_start passes resolved file values into the daemon runtime."""
    from argparse import Namespace

    from agt.daemon import main as daemon_main

    monkeypatch.setenv("HOME", str(tmp_path))
    pid_file = tmp_path / ".agt" / "daemon.pid"
    monkeypatch.setattr(daemon_main, "PID_FILE", pid_file)

    work = tmp_path / "work"
    allow = tmp_path / "allowed"
    cfg_path = tmp_path / ".agt" / "daemon.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(
        json.dumps(
            {
                "version": 1,
                "max_concurrent": 7,
                "agent_timeout": 900,
                "work_dir": str(work),
                "agent_cwd_allowlist": [str(allow)],
            }
        ),
        encoding="utf-8",
    )

    captured: dict[str, Any] = {}

    async def _fake_run_background(config):
        captured["config"] = config

    monkeypatch.setattr(daemon_main, "run_background", _fake_run_background)
    result = daemon_main.cmd_start(Namespace(server_url=None))

    assert result == 0
    config = captured["config"]
    assert config.max_concurrent_tasks == 7
    assert config.agent_timeout == 900
    assert config.work_dir_root == work
    assert config.agent_cwd_allowlist == [allow]


def test_daemon_status_and_stop_with_no_config_file(tmp_path, monkeypatch):
    """status/stop remain usable when ~/.agt/daemon.json is absent."""
    from argparse import Namespace

    from agt.daemon import main as daemon_main

    monkeypatch.setenv("HOME", str(tmp_path))
    pid_file = tmp_path / ".agt" / "daemon.pid"
    monkeypatch.setattr(daemon_main, "PID_FILE", pid_file)

    assert daemon_main.cmd_status(Namespace()) == 1
    assert daemon_main.cmd_stop(Namespace()) == 0
