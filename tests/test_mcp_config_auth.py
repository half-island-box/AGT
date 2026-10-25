"""Task-scoped MCP authentication config injection."""

from __future__ import annotations

from pathlib import Path

from agt.runtime.mcp_config import (
    MCP_BEARER_TOKEN_ENV,
    GlobalTomlMcpConfig,
    ProjectJsonMcpConfig,
    inject_mcp_config,
    reclaim_mcp_config,
)


def test_claude_project_config_uses_env_bearer_header(tmp_path) -> None:
    cwd = tmp_path / "project"
    cwd.mkdir()
    profile = ProjectJsonMcpConfig(".mcp.json")

    inject_mcp_config(
        profile,
        cwd,
        "http://127.0.0.1:8420/mcp/",
        bearer_token_env_var=MCP_BEARER_TOKEN_ENV,
    )

    text = (cwd / ".mcp.json").read_text(encoding="utf-8")
    assert "Bearer ${AGT_TASK_TOKEN}" in text
    assert "agt-server-secret" not in text

    reclaim_mcp_config(profile, cwd)
    assert not (cwd / ".mcp.json").exists()


def test_codex_global_config_uses_env_var_name_not_token(tmp_path) -> None:
    path = tmp_path / "codex" / "config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        'model = "gpt-5"\n\n[mcp_servers.user]\nurl = "http://user"\n',
        encoding="utf-8",
    )
    profile = GlobalTomlMcpConfig(
        path=path,
        state_dir=tmp_path / "state",
    )

    inject_mcp_config(
        profile,
        tmp_path,
        "http://127.0.0.1:8428/mcp",
        bearer_token_env_var=MCP_BEARER_TOKEN_ENV,
    )

    text = path.read_text(encoding="utf-8")
    assert "[mcp_servers.agt-catalog]" in text
    assert 'bearer_token_env_var = "AGT_TASK_TOKEN"' in text
    assert 'model = "gpt-5"' in text
    assert "[mcp_servers.user]" in text

    reclaim_mcp_config(profile, tmp_path)
    restored = path.read_text(encoding="utf-8")
    assert "[mcp_servers.agt-catalog]" not in restored
    assert 'model = "gpt-5"' in restored
    assert "[mcp_servers.user]" in restored
