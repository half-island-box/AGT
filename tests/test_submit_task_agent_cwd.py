"""Phase 4 workdir supplement: server persists ``agent_cwd`` from submit.

- Submit with ``agent_cwd``: server stores it on ``StepModel``.
- Status echoes ``agent_cwd`` back.
- Claim payload includes ``agent_cwd`` (Phase 4 workdir supplement).
- ``None`` agent_cwd stays ``None`` end-to-end (backward compatibility).
- Schema migration: pre-existing DB without the column gets ``ALTER TABLE``.
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "agent-cwd-test-secret"


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "ac.db"), secret=SECRET,
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


def test_submit_persists_agent_cwd(tmp_path) -> None:
    """Server stores ``agent_cwd`` verbatim; status echoes it back."""
    client, dispose = _build_app(tmp_path)
    try:
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi",
            "provider": "claude",
            "agent_cwd": "/home/u/project/agt/agt",
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]

        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.status_code == 200
        assert status.json()["agent_cwd"] == "/home/u/project/agt/agt"
    finally:
        dispose()


def test_submit_without_agent_cwd_is_null(tmp_path) -> None:
    """Old clients that don't send ``agent_cwd`` get NULL — no regression."""
    client, dispose = _build_app(tmp_path)
    try:
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]
        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.json()["agent_cwd"] is None
    finally:
        dispose()


def test_submit_null_agent_cwd_stays_null(tmp_path) -> None:
    """Explicit ``agent_cwd=None`` is preserved."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "remote-1", ["claude"])
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
            "target_daemon_id": "remote-1",
            "agent_cwd": None,
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]
        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.json()["agent_cwd"] is None
    finally:
        dispose()


def test_list_tasks_includes_agent_cwd(tmp_path) -> None:
    """``GET /api/tasks`` returns ``agent_cwd`` for each task."""
    client, dispose = _build_app(tmp_path)
    try:
        client.post("/api/tasks/submit", json={
            "prompt": "a", "provider": "claude",
            "agent_cwd": "/proj/a",
        })
        client.post("/api/tasks/submit", json={
            "prompt": "b", "provider": "claude",
        })
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        rows = resp.json()
        assert any(r["agent_cwd"] == "/proj/a" for r in rows)
        assert any(r["agent_cwd"] is None for r in rows)
    finally:
        dispose()


def test_claim_returns_agent_cwd_in_payload(tmp_path) -> None:
    """Claim payload includes the submitted agent_cwd for the daemon."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "remote-1", ["claude"])
        client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
            "target_daemon_id": "remote-1",
            "agent_cwd": "/proj/claim",
        })

        claim = client.post("/api/tasks/claim", json={
            "daemon_id": "remote-1",
            "runtime_ids": ["claude"],
            "max_concurrent": 1,
            "timeout_seconds": 1,
        })
        assert claim.status_code == 200
        tasks = claim.json()["tasks"]
        assert len(tasks) == 1
        assert tasks[0]["agent_cwd"] == "/proj/claim"
    finally:
        dispose()


def test_claim_without_agent_cwd_is_null(tmp_path) -> None:
    """Legacy tasks (no agent_cwd) claim with ``None`` — no regression."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "remote-1", ["claude"])
        client.post("/api/tasks/submit", json={
            "prompt": "hi", "provider": "claude",
            "target_daemon_id": "remote-1",
        })

        claim = client.post("/api/tasks/claim", json={
            "daemon_id": "remote-1",
            "runtime_ids": ["claude"],
            "max_concurrent": 1,
            "timeout_seconds": 1,
        })
        tasks = claim.json()["tasks"]
        assert tasks[0]["agent_cwd"] is None
    finally:
        dispose()