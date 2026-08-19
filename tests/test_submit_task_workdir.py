"""Phase 4 workdir: server persists ``work_dir`` from submit payloads.

- Free task (``target_daemon_id`` omitted): the server stores the value.
- Targeted task: the server stores the value as supplied (callers that
  know the target is local pass a real path; CLI submit paths send
  ``None`` when ``--no-work-dir`` is used so the receiving daemon
  supplies its own fallback).
- ``GET /api/tasks/{id}/status`` echoes ``work_dir`` back.
- ``GET /api/tasks?statuses=...`` lists tasks for ``agt task clean``.
- The schema accepts the new field (no validation regression).
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from agt.server.config import ServerConfig
from agt.server.database import make_engine
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base


SECRET = "workdir-test-secret"


def _build_app(tmp_path) -> tuple[TestClient, callable]:
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "workdir.db"), secret=SECRET,
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


def test_free_task_persists_work_dir(tmp_path) -> None:
    """Free task (no target): the server stores the supplied work_dir."""
    client, dispose = _build_app(tmp_path)
    try:
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi",
            "provider": "claude",
            "work_dir": "/home/u/project/.agt/tasks/abc",
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]

        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.status_code == 200
        assert status.json()["work_dir"] == "/home/u/project/.agt/tasks/abc"
    finally:
        dispose()


def test_remote_target_nulls_work_dir(tmp_path) -> None:
    """Targeted task: when ``work_dir`` is None (CLI submit path for a
    remote daemon), the server persists NULL — the receiving daemon
    supplies its own fallback path."""
    client, dispose = _build_app(tmp_path)
    try:
        _register(client, "remote-1", ["claude"])
        sub = client.post("/api/tasks/submit", json={
            "prompt": "hi",
            "provider": "claude",
            "target_daemon_id": "remote-1",
            "work_dir": None,
        })
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]

        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.json()["work_dir"] is None
    finally:
        dispose()


def test_list_tasks_filters_by_status(tmp_path) -> None:
    """``GET /api/tasks?statuses=completed,failed`` returns matching rows."""
    client, dispose = _build_app(tmp_path)
    try:
        # Submit two free tasks and one targeted.
        client.post("/api/tasks/submit", json={"prompt": "a", "provider": "claude"})
        _register(client, "remote-1", ["claude"])
        client.post("/api/tasks/submit", json={
            "prompt": "b", "provider": "claude", "target_daemon_id": "remote-1",
        })

        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        assert len(resp.json()) >= 2

        resp = client.get("/api/tasks", params={"statuses": "completed"})
        assert resp.status_code == 200
        # Newly submitted tasks are pending, so the filter should return [].
        assert resp.json() == []
    finally:
        dispose()


def test_submit_without_work_dir_field_is_backward_compatible(tmp_path) -> None:
    """Old clients that don't send ``work_dir`` get NULL (no regression)."""
    client, dispose = _build_app(tmp_path)
    try:
        sub = client.post("/api/tasks/submit", json={"prompt": "hi", "provider": "claude"})
        assert sub.status_code == 201
        task_id = sub.json()["task_id"]
        status = client.get(f"/api/tasks/{task_id}/status")
        assert status.json()["work_dir"] is None
    finally:
        dispose()