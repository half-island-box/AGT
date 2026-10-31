"""Goal shared-context storage, visibility, CAS, Brief and marker tests."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import pytest

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.models import (
    ArtifactModel,
    Base,
    ContextEntryModel,
    JobModel,
    StepModel,
)
from agt.server.services.claim_service import claim_steps_for_daemon
from agt.server.services.auth_service import TaskToken
from agt.server.services.context_service import (
    ContextCaller,
    build_context_brief,
    context_read_for_caller,
    context_write_for_caller,
)
from agt.server.services.report_service import record_step_result_for_daemon
from agt.server.services.spawn_service import spawn_children_for_token
from agt.task.spawn_intent import parse_context_markers


SECRET = "goal-shared-context-test-secret-0123456789"
DAEMON_ID = "daemon-context-1"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _factory(tmp_path):
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "context.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "sessions.json"),
    )
    engine = make_engine(cfg)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(_setup())
    return engine, make_session_factory(engine)


async def _seed(factory) -> None:
    now = _now()
    async with session_scope(factory) as db:
        db.add(JobModel(
            job_id="root",
            root_job_id="root",
            status="running",
            job_type="goal",
            provider="claude",
            created_at=now,
            updated_at=now,
        ))
        db.add(JobModel(
            job_id="child-a",
            root_job_id="root",
            parent_job_id="root",
            status="running",
            job_type="goal",
            provider="claude",
            created_at=now,
            updated_at=now,
        ))
        db.add(JobModel(
            job_id="child-b",
            root_job_id="root",
            parent_job_id="root",
            status="running",
            job_type="goal",
            provider="claude",
            created_at=now,
            updated_at=now,
        ))
        for job_id in ("root", "child-a", "child-b"):
            db.add(StepModel(
                step_id=f"step-{job_id}",
                job_id=job_id,
                provider="claude",
                prompt="work",
                status="running",
                daemon_id=DAEMON_ID,
                created_at=now,
                updated_at=now,
            ))


async def _caller(db, job_id: str) -> ContextCaller:
    job = await db.get(JobModel, job_id)
    step = await db.get(StepModel, f"step-{job_id}")
    assert job is not None and step is not None
    return ContextCaller(step=step, job=job, daemon_id=DAEMON_ID)


def test_authoritative_shared_and_proposal_permissions(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            child = await _caller(db, "child-a")
            root_result = await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="api-schema",
                kind="decision",
                content={"summary": "keep v1", "text": "Keep public API v1"},
                idempotency_key="root-1",
            )
            assert root_result["key"] == "shared/api-schema"

            with pytest.raises(ValueError) as exc:
                await context_write_for_caller(
                    db,
                    child,
                    scope="shared",
                    key="other",
                    kind="decision",
                    content={"text": "override"},
                    idempotency_key="child-bad",
                )
            assert exc.value.args[0]["code"] == "context_permission_denied"

            proposal = await context_write_for_caller(
                db,
                child,
                scope="shared",
                key="auth-note",
                kind="note",
                content={"text": "candidate"},
                idempotency_key="child-good",
            )
            assert proposal["key"].startswith("shared/proposal/child-a/")

        async with session_scope(factory) as db:
            child = await _caller(db, "child-a")
            sibling = await _caller(db, "child-b")
            read_child = await context_read_for_caller(db, child)
            read_sibling = await context_read_for_caller(db, sibling)
            child_keys = {item["key"] for item in read_child["entries"]}
            sibling_keys = {item["key"] for item in read_sibling["entries"]}
            assert "shared/api-schema" in child_keys
            assert "shared/api-schema" in sibling_keys
            assert any("proposal/child-a" in key for key in child_keys)
            assert any("proposal/child-a" in key for key in sibling_keys)

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_head_update_requires_matching_expected_id(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            first = await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="api-schema",
                kind="decision",
                content={"text": "v1"},
                idempotency_key="root-1",
            )
            with pytest.raises(ValueError) as exc:
                await context_write_for_caller(
                    db,
                    root,
                    scope="shared",
                    key="api-schema",
                    kind="decision",
                    content={"text": "v2"},
                    idempotency_key="root-2",
                )
            assert exc.value.args[0]["code"] == "context_head_conflict"
            updated = await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="api-schema",
                kind="decision",
                content={"text": "v2"},
                idempotency_key="root-3",
                expected_head_entry_id=first["entry_id"],
            )
            assert updated["entry_id"] != first["entry_id"]

        async with session_scope(factory) as db:
            rows = (await db.execute(
                ContextEntryModel.__table__.select().order_by(ContextEntryModel.entry_id)
            )).all()
            assert [row.status for row in rows] == ["superseded", "active"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_brief_contains_shared_decision_and_marks_omitted(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="api-schema",
                kind="decision",
                content={"summary": "keep v1", "text": "Keep public API v1"},
                idempotency_key="root-brief",
            )
        async with session_scope(factory) as db:
            job = await db.get(JobModel, "child-a")
            step = await db.get(StepModel, "step-child-a")
            assert job is not None and step is not None
            projected = await build_context_brief(db, step=step, job=job)
            assert projected is not None
            brief, file_parts = projected
            assert file_parts == []
            assert brief["entries"][0]["key"] == "shared/api-schema"
            assert brief["snapshot_id"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_context_refs_inject_unselected_fact(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            entry = await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="test-fact",
                kind="fact",
                content={"text": "all tests pass"},
                idempotency_key="root-fact",
            )
        async with session_scope(factory) as db:
            job = await db.get(JobModel, "child-a")
            step = await db.get(StepModel, "step-child-a")
            assert job is not None and step is not None
            step.input_message = json.dumps({
                "role": "user",
                "parts": [{
                    "type": "data",
                    "data": {"context_refs": [entry["key"]]},
                }],
            })
            projected = await build_context_brief(db, step=step, job=job)
            assert projected is not None
            brief, _ = projected
            assert brief["entries"][0]["key"] == entry["key"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_claim_injects_brief_into_structured_message(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="api-schema",
                kind="decision",
                content={"summary": "keep v1", "text": "Keep public API v1"},
                idempotency_key="root-claim",
            )
            child_step = await db.get(StepModel, "step-child-a")
            assert child_step is not None
            child_step.status = "pending"
            child_step.daemon_id = None
        async with session_scope(factory) as db:
            payloads = await claim_steps_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                max_tasks=1,
                timeout_seconds=1,
                runtime_ids=["claude"],
            )
            assert len(payloads) == 1
            parts = payloads[0].message["parts"]
            assert any(
                part.get("type") == "data" and "context_brief" in part.get("data", {})
                for part in parts
            )
        async with session_scope(factory) as db:
            step = await db.get(StepModel, "step-child-a")
            assert step is not None
            assert step.context_snapshot_id
            assert step.context_brief_json

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_artifact_refs_are_expanded_only_for_visible_lineage(tmp_path) -> None:
    engine, factory = _factory(tmp_path)
    artifact_id = "11111111-1111-4111-8111-111111111111"

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            db.add(ArtifactModel(
                artifact_id=artifact_id,
                job_id="child-a",
                step_id="step-child-a",
                producer_identity_id=None,
                name="report.md",
                mime_type="text/markdown",
                summary=None,
                status_flag="pass",
                size=4,
                sha256="a" * 64,
                local_path="/tmp/report.md",
                origin_daemon_id=DAEMON_ID,
                status="ready",
                created_at=_now(),
            ))
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            await context_write_for_caller(
                db,
                root,
                scope="shared",
                key="review",
                kind="decision",
                content={
                    "summary": "review complete",
                    "artifact_refs": [f"agt-artifact://{artifact_id}"],
                },
                idempotency_key="root-artifact",
            )
        async with session_scope(factory) as db:
            child_job = await db.get(JobModel, "child-a")
            child_step = await db.get(StepModel, "step-child-a")
            sibling_job = await db.get(JobModel, "child-b")
            sibling_step = await db.get(StepModel, "step-child-b")
            assert child_job and child_step and sibling_job and sibling_step
            child_brief = await build_context_brief(
                db, step=child_step, job=child_job
            )
            sibling_brief = await build_context_brief(
                db, step=sibling_step, job=sibling_job
            )
            assert child_brief is not None
            assert child_brief[1][0].uri == f"agt-artifact://{artifact_id}"
            assert sibling_brief is not None
            assert sibling_brief[1] == []

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_marker_parser_strips_and_keeps_ops() -> None:
    stdout = (
        "answer\n"
        'AGT-CONTEXT {"op":"write","scope":"job","key":"x","kind":"note","idempotency_key":"k","content":{"text":"y"}}\n'
    )
    parsed = parse_context_markers(stdout)
    assert len(parsed.ops) == 1
    assert "AGT-CONTEXT" not in parsed.cleaned_stdout
    assert "captured 1" in parsed.cleaned_stdout


def test_secret_like_content_is_rejected(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            root = await _caller(db, "root")
            with pytest.raises(ValueError) as exc:
                await context_write_for_caller(
                    db,
                    root,
                    scope="shared",
                    key="secret",
                    kind="fact",
                    content={"text": "password=not-a-real-secret"},
                    idempotency_key="secret-1",
                )
            assert exc.value.args[0]["code"] == "context_secret_rejected"

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_spawn_persists_context_refs_in_child_message(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        token = TaskToken.create(
            agent_id=DAEMON_ID,
            task_id="step-root",
            workspace_id="",
            secret=SECRET,
        )
        async with session_scope(factory) as db:
            result = await spawn_children_for_token(
                db,
                task_token=token,
                secret=SECRET,
                children=[{
                    "goal": "continue",
                    "context_refs": ["shared/api-schema"],
                    "return": "summary",
                }],
            )
            child_id = result["children"][0]["child_job_id"]
        async with session_scope(factory) as db:
            child_job = await db.get(JobModel, child_id)
            assert child_job is not None
            child_step = (
                await db.execute(
                    StepModel.__table__.select().where(
                        StepModel.job_id == child_id
                    )
                )
            ).first()
            message = json.loads(child_step.input_message)
            data = next(part["data"] for part in message["parts"] if part["type"] == "data")
            assert data["context_refs"] == ["shared/api-schema"]

    asyncio.run(_run())
    asyncio.run(engine.dispose())


def test_marker_context_ops_apply_before_terminal_report(tmp_path) -> None:
    engine, factory = _factory(tmp_path)

    async def _run() -> None:
        await _seed(factory)
        async with session_scope(factory) as db:
            result = await record_step_result_for_daemon(
                db,
                daemon_id=DAEMON_ID,
                step_id="step-child-a",
                exit_code=0,
                stdout="done",
                stderr="",
                duration=1.0,
                status="completed",
                session_id=None,
                resume_rejected=False,
                outcome="done",
                context_ops=[{
                    "op": "write",
                    "scope": "job",
                    "key": "finding",
                    "kind": "fact",
                    "idempotency_key": "marker-1",
                    "content": {"text": "found it"},
                }],
            )
            assert result["recorded"] is True
        async with session_scope(factory) as db:
            rows = (await db.execute(
                ContextEntryModel.__table__.select()
            )).all()
            assert len(rows) == 1
            assert json.loads(rows[0].content_json)["text"] == "found it"

    asyncio.run(_run())
    asyncio.run(engine.dispose())
