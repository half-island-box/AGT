"""Step 3.6 — daemon-targeted routing tests.

Verifies that ``target_daemon_id`` on submit pins a task to one daemon:

- A targeted task is claimable only by the daemon it names; other daemons
  (even with a matching provider) never see it.
- A free task (no ``target_daemon_id``) stays claimable by any matching
  daemon — both classes coexist in the same queue.
- Submit validation rejects a target that has not registered the provider.
- A terminal task_id cannot be resubmitted (Steps are immutable → 409).
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "target-routing-test-secret"


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    """Boot a TestClient on a fresh tmp DB; returns ``(client, dispose)``."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "target.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(_setup())
    cfg._engine_ref = engine  # type: ignore[attr-defined]

    app = create_app(cfg)
    client = TestClient(app)

    def _dispose() -> None:
        asyncio.run(cfg._engine_ref.dispose())  # type: ignore[attr-defined]

    return client, _dispose


def _register(client: TestClient, daemon_id: str, providers: list[str]) -> None:
    """Register ``providers`` for ``daemon_id`` via the HTTP API."""
    resp = client.post("/api/runtimes/register", json={
        "daemon_id": daemon_id,
        "runtimes": [
            {
                "name": p, "type": p, "version": "1.0",
                "status": "online", "device_name": "test-device",
            }
            for p in providers
        ],
    })
    assert resp.status_code == 201


def _claim(client: TestClient, daemon_id: str, providers: list[str]) -> list[dict]:
    resp = client.post("/api/tasks/claim", json={
        "daemon_id": daemon_id, "max_concurrent": 1,
        "timeout_seconds": 0, "runtime_ids": providers,
    })
    assert resp.status_code == 200
    return resp.json()["tasks"]


# ── Targeted vs free routing ──────────────────────────────────────


def test_targeted_task_claimed_only_by_target_daemon(tmp_path) -> None:
    """A targeted task is invisible to other daemons, even with the provider."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d-target", ["claude"])
        _register(client, "d-other", ["claude"])

        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude", "target_daemon_id": "d-target",
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]

        # Another daemon with claude cannot claim the targeted task.
        assert _claim(client, "d-other", ["claude"]) == []

        # The target daemon claims it.
        tasks = _claim(client, "d-target", ["claude"])
        assert [t["task_id"] for t in tasks] == [task_id]
    finally:
        dispose()


def test_free_task_claimable_by_any_daemon(tmp_path) -> None:
    """No target → any provider-matching daemon can claim (unchanged)."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d1", ["claude"])
        _register(client, "d2", ["claude"])

        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
        })
        assert sub.status_code == 201

        # Either daemon can claim the free task (here: d2 claims first).
        tasks = _claim(client, "d2", ["claude"])
        assert len(tasks) == 1
        assert tasks[0]["prompt"] == "hi"
    finally:
        dispose()


def test_targeted_and_free_tasks_coexist(tmp_path) -> None:
    """A free task and a targeted task coexist; targeted daemon skips free
    task meant for someone else when free task is already claimed."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d-target", ["claude"])
        _register(client, "d-other", ["claude"])

        client.post("/api/tasks/submit", json={
            "prompt": "free", "provider": "claude",
        })
        client.post("/api/tasks/submit", json={
            "prompt": "pinned", "provider": "claude", "target_daemon_id": "d-target",
        })

        # d-other claims the oldest task — the free one (target is NULL).
        free = _claim(client, "d-other", ["claude"])
        assert len(free) == 1
        assert free[0]["prompt"] == "free"

        # The targeted task remains for d-target only.
        pinned = _claim(client, "d-target", ["claude"])
        assert len(pinned) == 1
        assert pinned[0]["prompt"] == "pinned"
    finally:
        dispose()


# ── Submit validation ─────────────────────────────────────────────


def test_submit_targeted_rejects_unknown_daemon(tmp_path) -> None:
    """Targeting a daemon that registered nothing → 400."""
    client, dispose = _build_app(tmp_path)
    try:
        resp = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude", "target_daemon_id": "d-nonexistent",
        })
        assert resp.status_code == 400
    finally:
        dispose()


def test_submit_targeted_rejects_missing_provider(tmp_path) -> None:
    """Targeting a daemon that lacks the requested provider → 400."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d-target", ["codex"])  # only codex, no claude
        resp = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude", "target_daemon_id": "d-target",
        })
        assert resp.status_code == 400
    finally:
        dispose()


def test_submit_targeted_ok_and_status_reflects_target(tmp_path) -> None:
    """Valid targeted submit → 201; status echoes daemon_id/target_daemon_id."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d-target", ["claude"])
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude", "target_daemon_id": "d-target",
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]

        status = client.get(f"/api/tasks/{task_id}/status").json()
        assert status["target_daemon_id"] == "d-target"
        assert status["status"] == "pending"
    finally:
        dispose()


# ── Terminal task_id resubmit (immutable Step) ────────────────────


def test_terminal_task_resubmit_conflicts(tmp_path) -> None:
    """A completed task_id cannot be resubmitted → 409 (no reopen).

    Phase 1 makes a Step immutable: session continuity is expressed as a
    new Step under the same Job, not by reopening a terminal task_id.
    """
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "d1", ["claude"])

        sub = client.post("/api/tasks/submit", json={
            "prompt": "turn 1", "provider": "claude", "task_id": "t-reuse",
        })
        assert sub.status_code == 201

        # Claim + report to drive it to a terminal state.
        _claim(client, "d1", ["claude"])
        report = client.post("/api/tasks/t-reuse/report", json={
            "daemon_id": "d1", "task_id": "t-reuse",
            "exit_code": 0, "stdout": "done", "stderr": "",
            "duration": 1.0, "status": "completed",
        })
        assert report.status_code == 200
        assert client.get("/api/tasks/t-reuse/status").json()["status"] == "completed"

        # Resubmit the same task_id → 409 (terminal task_ids no longer reopen).
        again = client.post("/api/tasks/submit", json={
            "prompt": "turn 2", "provider": "claude", "task_id": "t-reuse",
        })
        assert again.status_code == 409
    finally:
        dispose()


def test_active_task_resubmit_conflicts(tmp_path) -> None:
    """A pending task_id cannot be double-submitted → 409."""
    client, dispose = _build_app(tmp_path)
    try:
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude", "task_id": "t-active",
        })
        assert sub.status_code == 201

        dup = client.post("/api/tasks/submit", json={
            "prompt": "again", "provider": "claude", "task_id": "t-active",
        })
        assert dup.status_code == 409
    finally:
        dispose()
