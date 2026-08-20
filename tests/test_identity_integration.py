"""Phase 4 Step 1 — end-to-end integration test.

Boots a REAL agt-server (uvicorn on an ephemeral port, in a daemon
thread), then drives the REAL ``agt identity`` CLI commands against it
over actual HTTP:

    create → AGENT.md on disk → list shows it → show details →
    update leaves AGENT.md untouched → sync-files rewrites it

AGENT.md writes are redirected to tmp_path by monkeypatching
``routes.identities._root_dir`` (the routes run inside the server
thread, but the module attribute is patched process-wide).
"""

from __future__ import annotations

import threading
import time

import uvicorn
from click.testing import CliRunner

from agt.cli.main import cli
from agt.server.config import ServerConfig
from agt.server.main import create_app


SECRET = "identity-integration-secret"
DID = "did:agt:local:code-reviewer"


def _boot_server(tmp_path) -> tuple[str, object]:
    """Start uvicorn on an ephemeral port; return ``(base_url, server)``."""
    cfg = ServerConfig(
        host="127.0.0.1", port=0,  # 0 → ephemeral port
        db_path=str(tmp_path / "integration.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    app = create_app(cfg)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=0, log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 10.0
    while not server.started:
        if time.monotonic() > deadline:
            raise TimeoutError("integration server failed to start")
        time.sleep(0.05)

    port = server.servers[0].sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}", server


def _stop(server) -> None:
    server.should_exit = True
    # uvicorn's server.run thread exits on its own; give it a moment.
    time.sleep(0.2)


def test_cli_create_to_agent_md_end_to_end(tmp_path, monkeypatch) -> None:
    from agt.server.routes import identities as identities_routes
    monkeypatch.setattr(identities_routes, "_root_dir", lambda: tmp_path)

    base_url, server = _boot_server(tmp_path)
    runner = CliRunner()
    md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENT.md"
    try:
        # 1. create → 201 over real HTTP, AGENT.md written.
        created = runner.invoke(cli, [
            "identity", "create",
            "--name", "Code Reviewer",
            "--capability", "code-review",
            "--capability", "doc-style-audit",
            "--description", "Python code review",
            "--server-url", base_url,
        ])
        assert created.exit_code == 0, created.output
        assert DID in created.output
        assert md.exists()
        content = md.read_text()
        assert "# Code Reviewer" in content
        assert DID in content
        assert "- code-review" in content

        # 2. list shows the identity.
        listed = runner.invoke(cli, ["identity", "list", "--server-url", base_url])
        assert listed.exit_code == 0, listed.output
        assert DID in listed.output

        # 3. show renders details.
        shown = runner.invoke(cli, ["identity", "show", DID, "--server-url", base_url])
        assert shown.exit_code == 0, shown.output
        assert "Code Reviewer" in shown.output
        assert "doc-style-audit" in shown.output

        # 4. update does NOT touch AGENT.md (manual edits preserved).
        md.write_text("MANUAL EDITS ONLY")
        updated = runner.invoke(cli, [
            "identity", "update", DID, "--name", "Renamed",
            "--server-url", base_url,
        ])
        assert updated.exit_code == 0, updated.output
        assert md.read_text() == "MANUAL EDITS ONLY"

        # 5. sync-files regenerates from database truth (with the new name).
        synced = runner.invoke(cli, [
            "identity", "sync-files", DID, "--server-url", base_url,
        ])
        assert synced.exit_code == 0, synced.output
        regenerated = md.read_text()
        assert "MANUAL EDITS" not in regenerated
        assert "# Renamed" in regenerated
    finally:
        _stop(server)
