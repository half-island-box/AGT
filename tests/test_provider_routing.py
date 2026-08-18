"""Step 3.4 — provider-aware claim tests.

Verifies that ``runtime_ids`` actually filters pending tasks by provider:

- A daemon declaring only ``claude`` cannot claim ``codex`` tasks.
- A daemon declaring multiple providers can claim any of them.
- Empty ``runtime_ids`` returns no tasks (explicit "I have nothing").
- HTTP path exercises the shared ``claim_tasks_for_daemon`` service; the
  reverse-dial WS path is covered in ``test_reverse_dial.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy import delete
from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, TaskModel


SECRET = "step-3-4-test-secret"


def _build_app_with_tasks(
    tasks: list[tuple[str, str]],  # [(task_id, provider), ...]
) -> tuple[TestClient, callable]:
    """Boot a TestClient with a fresh DB seeded with the given tasks.

    Returns ``(client, dispose)`` — caller must invoke ``dispose()`` to
    clean up.
    """
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path="/tmp/_provider_routing.db", secret=SECRET,
        sessions_json_path="/tmp/_nonexistent_sessions_for_test.json",
    )

    async def _setup() -> None:
        engine = make_engine(cfg)
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        factory = make_session_factory(engine)
        async with session_scope(factory) as db:
            await db.execute(delete(TaskModel))
            now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            for tid, provider in tasks:
                db.add(TaskModel(
                    task_id=tid, provider=provider, prompt="x",
                    status="pending", created_at=now, updated_at=now,
                ))
            await db.commit()
        # Stash on cfg for the dispose closure.
        cfg._engine_ref = engine  # type: ignore[attr-defined]

    asyncio.run(_setup())

    app = create_app(cfg)
    client = TestClient(app)

    async def _dispose() -> None:
        await cfg._engine_ref.dispose()  # type: ignore[attr-defined]

    def _dispose_sync() -> None:
        asyncio.run(_dispose())

    return client, _dispose_sync


# ── HTTP path ─────────────────────────────────────────────────────


def test_http_claude_only_daemon_skips_codex_task() -> None:
    """Daemon declaring ['claude'] cannot claim a codex task."""
    client, dispose = _build_app_with_tasks(
        [("t-codex", "codex"), ("t-claude", "claude")],
    )
    try:
        resp = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": ["claude"],
        })
        assert resp.status_code == 200
        tasks = resp.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["task_id"] == "t-claude"
        assert tasks[0]["provider"] == "claude"
    finally:
        dispose()


def test_http_multi_provider_daemon_claims_any_match() -> None:
    """Daemon with both claude and codex can claim either."""
    client, dispose = _build_app_with_tasks(
        [("t-codex", "codex"), ("t-claude", "claude")],
    )
    try:
        resp = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": ["claude", "codex"],
        })
        tasks = resp.json()["tasks"]
        # oldest first → codex (created first in our seed order)
        assert tasks[0]["provider"] in {"claude", "codex"}
        assert tasks[0]["task_id"] == "t-codex"
    finally:
        dispose()


def test_http_empty_runtime_ids_returns_no_tasks() -> None:
    """Explicit empty list → no tasks (daemon declared no capabilities)."""
    client, dispose = _build_app_with_tasks(
        [("t1", "claude"), ("t2", "codex")],
    )
    try:
        resp = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": [],
        })
        assert resp.json()["tasks"] == []
    finally:
        dispose()


def test_http_omitted_runtime_ids_returns_no_tasks() -> None:
    """Omitting runtime_ids field is equivalent to [] — daemon must declare."""
    client, dispose = _build_app_with_tasks([("t1", "claude")])
    try:
        resp = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            # no runtime_ids key
        })
        assert resp.json()["tasks"] == []
    finally:
        dispose()


def test_http_filter_does_not_affect_existing_pending_state() -> None:
    """Filter only picks matching providers; remaining pendings stay queued."""
    client, dispose = _build_app_with_tasks(
        [("t-c1", "claude"), ("t-c2", "claude"), ("t-x1", "codex")],
    )
    try:
        # First claim picks the oldest claude task.
        resp = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": ["claude"],
        })
        first = resp.json()["tasks"]
        assert len(first) == 1
        assert first[0]["task_id"] == "t-c1"

        # Second claim picks the next claude; codex stays queued.
        resp2 = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": ["claude"],
        })
        second = resp2.json()["tasks"]
        assert len(second) == 1
        assert second[0]["task_id"] == "t-c2"

        # Third claim: nothing left for claude.
        resp3 = client.post("/api/tasks/claim", json={
            "daemon_id": "d1", "max_concurrent": 1, "timeout_seconds": 0,
            "runtime_ids": ["claude"],
        })
        assert resp3.json()["tasks"] == []
    finally:
        dispose()
# ── Daemon.runtime_providers() ─────────────────────────────────────


def test_daemon_runtime_providers_reflects_probe_results() -> None:
    """``Daemon.runtime_providers()`` lists keys of the runtime map."""
    from agt.daemon.daemon import Daemon
    from agt.runtime.models import AgentEntry, Runtime

    daemon = Daemon(config=__import__("agt.daemon.config", fromlist=["DaemonConfig"]).DaemonConfig())
    # Inject two fake runtimes.
    claude_entry = AgentEntry(
        command="claude", path="/usr/bin/claude",
        version="1.0.0", provider="claude", display_name="Claude",
    )
    codex_entry = AgentEntry(
        command="codex", path="/usr/bin/codex",
        version="0.5.0", provider="codex", display_name="Codex",
    )
    daemon._runtimes["claude"] = Runtime(
        name="Claude", type="claude", version="1.0.0", status="online",
        agent_entry=claude_entry, device_name="test",
    )
    daemon._runtimes["codex"] = Runtime(
        name="Codex", type="codex", version="0.5.0", status="online",
        agent_entry=codex_entry, device_name="test",
    )
    assert sorted(daemon.runtime_providers()) == ["claude", "codex"]

    daemon._runtimes.pop("codex")
    assert daemon.runtime_providers() == ["claude"]