"""MCP-first Goal spawning, authentication, idempotency and lifecycle tests."""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.mcp_server import spawn_subagents_impl
from agt.server.models import Base, IdentityModel, JobModel, SpawnBatchModel, StepModel
from agt.server.services.claim_service import claim_steps_for_daemon
from agt.server.services.report_service import record_step_result_for_daemon
from agt.server.services.spawn_service import spawn_children_for_token
from agt.server.services.auth_service import TaskToken


SECRET = "mcp-goal-spawn-test-secret-0123456789"
DAEMON_ID = "daemon-goal-1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _factory(tmp_path):
    from agt.server.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "goal-spawn.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "sessions.json"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    return engine, make_session_factory(engine)


def _token(step_id: str = "step-root") -> str:
    return TaskToken.create(
        agent_id=DAEMON_ID,
        task_id=step_id,
        workspace_id="",
        secret=SECRET,
    )


async def _seed_goal(
    factory,
    *,
    job_type: str = "goal",
    step_status: str = "running",
    budget: dict | None = None,
) -> None:
    now = _now()
    meta = json.dumps({
        "mode": "goal",
        "budget": budget or {
            "max_depth": 5,
            "max_descendants": 32,
            "max_batch": 8,
        },
    })
    async with session_scope(factory) as db:
        db.add(JobModel(
            job_id="root",
            root_job_id="root",
            status="running",
            job_type=job_type,
            provider="claude",
            goal_meta=meta,
            created_at=now,
            updated_at=now,
        ))
        db.add(StepModel(
            step_id="step-root",
            job_id="root",
            provider="claude",
            prompt="goal",
            status=step_status,
            daemon_id=DAEMON_ID,
            created_at=now,
            updated_at=now,
        ))


def _child(index: int = 0) -> dict:
    return {
        "goal": f"child {index}",
        "inputs": [],
        "return": "result",
    }


def test_spawn_child_keeps_identity_without_binding(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        now = _now()
        async with session_scope(factory) as db:
            db.add(
                IdentityModel(
                    did="did:agt:reviewer",
                    daemon_id="local",
                    name="Reviewer",
                    description="",
                    capabilities="[]",
                    status="active",
                    visibility="private",
                    created_by="user",
                    created_at=now,
                    updated_at=now,
                )
            )
        async with session_scope(factory) as db:
            spawned = await spawn_children_for_token(
                db,
                task_token=_token(),
                secret=SECRET,
                children=[
                    {
                        **_child(),
                        "identity": "did:agt:reviewer",
                    }
                ],
            )
        assert spawned["accepted"] == 1
        child_id = spawned["children"][0]["child_job_id"]
        async with session_scope(factory) as db:
            child = await db.get(JobModel, child_id)
            assert child is not None
            assert child.identity_did == "did:agt:reviewer"
            assert child.status != "failed"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_spawn_creates_children_and_replays_idempotently(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        token = _token()
        async with session_scope(factory) as db:
            first = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=[_child(1), _child(2)],
                idempotency_key="batch-1",
            )
        assert first["ok"] is True
        assert first["accepted"] == 2
        assert first["duplicate"] is False

        async with session_scope(factory) as db:
            second = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=[_child(1), _child(2)],
                idempotency_key="batch-1",
            )
            children = (
                await db.execute(
                    select(JobModel).where(JobModel.parent_job_id == "root")
                )
            ).scalars().all()
            batches = (
                await db.execute(select(SpawnBatchModel))
            ).scalars().all()
        assert second["ok"] is True
        assert second["duplicate"] is True
        assert second["spawn_id"] == first["spawn_id"]
        assert len(children) == 2
        assert len(batches) == 1
        assert {c.target_daemon_id for c in children} == {DAEMON_ID}

        async with session_scope(factory) as db:
            third = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=[_child(1), _child(2)],
                idempotency_key="different-retry-key",
            )
        assert third["duplicate"] is True
        assert third["spawn_id"] == first["spawn_id"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_idempotency_key_conflict_is_rejected(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        token = _token()
        async with session_scope(factory) as db:
            await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=[_child(1)],
                idempotency_key="same-key",
            )
        async with session_scope(factory) as db:
            with pytest.raises(ValueError) as exc:
                await spawn_children_for_token(
                    db,
                    task_token=token,
                    secret=SECRET,
                    children=[_child(2)],
                    idempotency_key="same-key",
                )
        assert exc.value.args[0]["code"] == "idempotency_conflict"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_budget_excess_is_reported_not_silently_truncated(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(
            factory,
            budget={"max_depth": 5, "max_descendants": 32, "max_batch": 1},
        )
        async with session_scope(factory) as db:
            result = await spawn_children_for_token(
                db,
                task_token=_token(),
                secret=SECRET,
                children=[_child(1), _child(2)],
            )
        assert result["accepted"] == 1
        assert result["rejected"][0]["code"] == "batch_too_large"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_empty_children_is_rejected(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            with pytest.raises(ValueError) as exc:
                await spawn_children_for_token(
                    db,
                    task_token=_token(),
                    secret=SECRET,
                    children=[],
                )
        assert exc.value.args[0]["code"] == "invalid_spawn_request"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_token_cannot_spawn_for_another_daemon(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            with pytest.raises(ValueError) as exc:
                await spawn_children_for_token(
                    db,
                    task_token=_token(),
                    secret=SECRET,
                    children=[_child()],
                    trusted_daemon_id="daemon-other",
                )
        assert exc.value.args[0]["code"] == "task_not_owned"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_non_goal_job_is_rejected(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory, job_type="task")
        async with session_scope(factory) as db:
            with pytest.raises(ValueError) as exc:
                await spawn_children_for_token(
                    db,
                    task_token=_token(),
                    secret=SECRET,
                    children=[_child()],
                )
        assert exc.value.args[0]["code"] == "not_goal_job"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_mcp_impl_extracts_bearer_and_returns_structured_error(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        ok = await spawn_subagents_impl(
            factory,
            SECRET,
            f"Bearer {_token()}",
            [_child()],
        )
        bad = await spawn_subagents_impl(
            factory,
            SECRET,
            None,
            [_child()],
        )
        assert ok["ok"] is True
        assert bad["ok"] is False
        assert bad["error"]["code"] == "unauthenticated"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_parent_report_waits_then_synthesizes_once(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            spawned = await spawn_children_for_token(
                db,
                task_token=_token(),
                secret=SECRET,
                children=[_child()],
            )
        child_job_id = spawned["children"][0]["child_job_id"]
        async with session_scope(factory) as db:
            child_job = await db.get(JobModel, child_job_id)
            assert child_job is not None and len(child_job.steps) == 1
            child_step_id = child_job.steps[0].step_id

        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=0,
                stdout="",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
            )

        async with session_scope(factory) as db:
            parent = await db.get(JobModel, "root")
            parent_step = await db.get(StepModel, "step-root")
            assert parent is not None and parent.status == "waiting_children"
            assert parent_step is not None
            assert parent_step.outcome == "spawn_subjob"

        async with session_scope(factory) as db:
            claimed = await claim_steps_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                max_tasks=10,
                timeout_seconds=0,
                runtime_ids=["claude"],
            )
        assert child_step_id in {step.task_id for step in claimed}

        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id=child_step_id,
                exit_code=0,
                stdout="child done",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
            )

        async with session_scope(factory) as db:
            parent = await db.get(JobModel, "root")
            assert parent is not None and parent.status == "running"
            assert len(parent.steps) == 2
            assert parent.steps[-1].status == "pending"

            # Replaying the parent's terminal report after synthesis was
            # created must not move it back to waiting_children.
            replay = await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=0,
                stdout="duplicate parent report",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
            )
            assert replay["recorded"] is False
            assert replay["reason"] == "already_terminal"

            # Duplicate child report must not append a second synthesis Step.
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id=child_step_id,
                exit_code=0,
                stdout="child done again",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
            )

        async with session_scope(factory) as db:
            count = (
                await db.execute(
                    select(func.count()).select_from(StepModel).where(
                        StepModel.job_id == "root"
                    )
                )
            ).scalar_one()
        assert count == 2

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_retry_after_parent_finishes_replays_existing_batch(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        token = _token()
        children = [_child()]
        async with session_scope(factory) as db:
            first = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=children,
                idempotency_key="replay-key",
            )
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=0,
                stdout="",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
            )
        async with session_scope(factory) as db:
            replay = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=children,
                idempotency_key="replay-key",
            )
        assert replay["duplicate"] is True
        assert replay["spawn_id"] == first["spawn_id"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_failed_parent_cancels_spawned_children(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            spawned = await spawn_children_for_token(
                db,
                task_token=_token(),
                secret=SECRET,
                children=[_child()],
            )
        child_job_id = spawned["children"][0]["child_job_id"]

        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=1,
                stdout="",
                stderr="parent failed",
                duration=0.1,
                status="failed",
                session_id=None,
                resume_rejected=False,
                outcome="error",
            )

        async with session_scope(factory) as db:
            parent = await db.get(JobModel, "root")
            child = await db.get(JobModel, child_job_id)
            assert parent is not None and parent.status == "failed"
            assert child is not None and child.status == "cancelled"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_spawn_wins_if_parent_reports_needs_input(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            await spawn_children_for_token(
                db,
                task_token=_token(),
                secret=SECRET,
                children=[_child()],
            )

        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=0,
                stdout="",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="needs_input",
                outcome_payload={"ask": {"question": "continue?"}},
            )

        async with session_scope(factory) as db:
            parent = await db.get(JobModel, "root")
            parent_step = await db.get(StepModel, "step-root")
            assert parent is not None and parent.status == "waiting_children"
            assert parent_step is not None
            assert parent_step.outcome == "spawn_subjob"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_marker_fallback_uses_same_spawn_service(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        async with session_scope(factory) as db:
            await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-root",
                exit_code=0,
                stdout="",
                stderr="",
                duration=0.1,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="spawn_subjob",
                outcome_payload={"children": [_child()]},
            )

        async with session_scope(factory) as db:
            parent = await db.get(JobModel, "root")
            children = (
                await db.execute(
                    select(JobModel).where(JobModel.parent_job_id == "root")
                )
            ).scalars().all()
            batches = (
                await db.execute(select(SpawnBatchModel))
            ).scalars().all()
        assert parent is not None and parent.status == "waiting_children"
        assert len(children) == 1
        assert len(batches) == 1

    asyncio.run(_run())
    asyncio.run(engine.dispose())


@dataclass
class _StubLink:
    response: dict
    calls: list[tuple[str, dict, int]] = field(default_factory=list)
    closed: bool = False
    supports_rpc_v1: bool = True

    async def call(self, method: str, body: dict, timeout_ms: int) -> dict:
        self.calls.append((method, body, timeout_ms))
        return self.response


@dataclass
class _StubWsServer:
    current_link: _StubLink | None


def test_remote_proxy_forwards_token_and_args() -> None:
    from agt.daemon.mcp_proxy import spawn_subagents_proxy_impl

    async def _run() -> None:
        link = _StubLink({"ok": True, "accepted": 1})
        result = await spawn_subagents_proxy_impl(
            _StubWsServer(link),
            f"Bearer {_token()}",
            [_child()],
            "remote-key",
        )
        assert result["ok"] is True
        method, body, _ = link.calls[0]
        assert method == "spawn.create"
        assert body["task_token"] == _token()
        assert body["children"] == [_child()]
        assert body["idempotency_key"] == "remote-key"

    asyncio.run(_run())


def test_reverse_dial_handler_uses_trusted_daemon_identity(tmp_path) -> None:
    from agt.server.daemon_link import DaemonLink

    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed_goal(factory)
        link = DaemonLink(
            daemon_id=DAEMON_ID,
            url="ws://unused",
            secret=SECRET,
            session_factory=factory,
        )
        response = await link._handle_spawn_create({
            "task_token": _token(),
            "children": [_child()],
        })
        assert response["ok"] is True
        assert response["accepted"] == 1

        other_link = DaemonLink(
            daemon_id="daemon-other",
            url="ws://unused",
            secret=SECRET,
            session_factory=factory,
        )
        with pytest.raises(ValueError) as exc:
            await other_link._handle_spawn_create({
                "task_token": _token(),
                "children": [_child(2)],
            })
        assert exc.value.args[0]["code"] == "task_not_owned"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_mcp_tool_schema_hides_context_parameter() -> None:
    from agt.daemon.mcp_proxy import build_proxy_mcp
    from agt.server.mcp_server import build_catalog_mcp

    class _Server:
        current_link = None

    async def _run() -> None:
        for server in (
            build_catalog_mcp(object(), {}, SECRET),
            build_proxy_mcp(_Server()),
        ):
            tools = await server.list_tools()
            spawn = next(tool for tool in tools if tool.name == "spawn_subagents")
            properties = spawn.inputSchema["properties"]
            assert "children" in properties
            assert "idempotency_key" in properties
            assert "ctx" not in properties
            child_schema = spawn.inputSchema["$defs"]["SpawnChildInput"]
            assert child_schema["additionalProperties"] is False
            assert set(child_schema["properties"]) >= {
                "goal",
                "identity",
                "inputs",
                "return",
                "daemon_id",
                "provider",
            }

    asyncio.run(_run())


def test_streamable_http_spawn_uses_request_bearer_token(tmp_path) -> None:
    import httpx
    import uvicorn
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    from agt.server.config import ServerConfig
    from agt.server.main import create_app

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    cfg = ServerConfig(
        host="127.0.0.1",
        port=port,
        db_path=str(tmp_path / "mcp-http.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _run() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await _seed_goal(factory)
        app = create_app(cfg)
        server = uvicorn.Server(uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
        ))
        task = asyncio.create_task(server.serve())
        try:
            for _ in range(100):
                if server.started:
                    break
                await asyncio.sleep(0.02)
            assert server.started
            token = _token()
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client:
                async with streamable_http_client(
                    f"http://127.0.0.1:{port}/mcp/",
                    http_client=http_client,
                ) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "spawn_subagents",
                            {"children": [_child()]},
                        )
            assert result.structuredContent["ok"] is True
            assert result.structuredContent["accepted"] == 1
        finally:
            server.should_exit = True
            await task
            await engine.dispose()

    asyncio.run(_run())
