"""Server-authoritative interruption of queued and running Chat turns."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory, session_scope
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, SessionModel
from agt.server.routes.jobs import job_stop
from agt.server.routes.tasks import task_cancellations
from agt.server.schemas import CancellationRequest
from agt.server.services.claim_service import claim_steps_for_daemon
from agt.server.services.job_service import append_step, create_job
from agt.server.services.report_service import record_step_result_for_daemon
from agt.server.stream_hub import default_hub


def _make_factory(tmp_path):
    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "chat-stop.db"),
        secret="chat-stop-test-secret",
        sessions_json_path=str(tmp_path / "sessions.json"),
    )
    engine = make_engine(cfg)

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)

    asyncio.run(setup())
    return make_session_factory(engine), engine


def _request(daemon_links=None):
    return SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                daemon_links=daemon_links or {},
                cancel_notify_tasks=set(),
            )
        )
    )


def test_stop_interrupts_queued_chat_turn(tmp_path):
    factory, engine = _make_factory(tmp_path)

    async def run():
        async with session_scope(factory) as db:
            job = await create_job(
                db,
                provider="claude",
                prompt="first",
                target_daemon_id="d1",
                job_type="chat",
            )
            await db.commit()
            assert job.status == "queued"
            assert job.steps[0].status == "pending"

            sub = default_hub.subscribe(job.steps[0].step_id)
            response = await job_stop(job.job_id, _request(), db)
            await db.commit()
            default_hub.unsubscribe(job.steps[0].step_id, sub)

            assert response["status"] == "waiting_input"
            assert response["interrupted_task_ids"] == [job.steps[0].step_id]
            assert job.steps[0].status == "cancelled"
            assert job.steps[0].exit_code is None
            event = sub.queue.get_nowait()
            assert event["type"] == "done"
            assert event["status"] == "cancelled"

            follow_up = await append_step(db, job.job_id, "second")
            await db.commit()
            assert follow_up.status == "pending"
            assert job.status == "running"

    asyncio.run(run())
    asyncio.run(engine.dispose())


def test_stop_publishes_done_and_allows_follow_up_without_late_report(tmp_path):
    factory, engine = _make_factory(tmp_path)
    task_id = None

    async def run():
        nonlocal task_id
        async with session_scope(factory) as db:
            job = await create_job(
                db,
                provider="claude",
                prompt="first",
                target_daemon_id="d1",
                job_type="chat",
            )
            first = job.steps[0]
            first.status = "running"
            first.daemon_id = "d1"
            first.stdout = "partial answer"
            await db.commit()
            task_id = first.step_id

            sub = default_hub.subscribe(task_id)
            response = await job_stop(job.job_id, _request(), db)
            await db.commit()
            assert response["status"] == "waiting_input"
            event = sub.queue.get_nowait()
            default_hub.unsubscribe(task_id, sub)
            assert event["type"] == "done"
            assert event["status"] == "cancelled"
            assert event["stdout"] == "partial answer"

            follow_up = await append_step(db, job.job_id, "second")
            await db.commit()
            claimed = await claim_steps_for_daemon(
                db,
                daemon_id="d1",
                max_tasks=1,
                timeout_seconds=0,
                runtime_ids=["claude"],
            )
            await db.commit()
            assert [payload.task_id for payload in claimed] == [follow_up.step_id]
            assert follow_up.status == "running"

            # A late old-turn report is transport cleanup only. It cannot
            # mutate the old Step or fail the now-running follow-up turn.
            result = await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id=task_id,
                exit_code=0,
                stdout="late full answer",
                stderr="",
                duration=1.0,
                status="completed",
                session_id="late-session",
                resume_rejected=False,
            )
            await db.commit()
            assert result["recorded"] is False
            assert result["reason"] == "task_cancelled"
            assert first.stdout == "partial answer"
            assert first.session_id is None
            assert follow_up.status == "running"
            assert job.status == "running"

            result = await record_step_result_for_daemon(
                db,
                daemon_id="d1",
                step_id=task_id,
                exit_code=-1,
                stdout="late cancelled answer",
                stderr="",
                duration=2.0,
                status="cancelled",
                session_id="cancelled-session",
                resume_rejected=False,
            )
            await db.commit()
            assert result["recorded"] is False
            assert first.stdout == "partial answer"
            assert first.session_id == "cancelled-session"
            pointer = (
                await db.execute(
                    select(SessionModel).where(SessionModel.job_id == job.job_id)
                )
            ).scalar_one()
            assert pointer.session_id == "cancelled-session"

            cancelled = await task_cancellations(
                CancellationRequest(daemon_id="d1"), db
            )
            assert cancelled.cancelled_task_ids == [task_id]
            cancelled_again = await task_cancellations(
                CancellationRequest(daemon_id="d1"), db
            )
            assert cancelled_again.cancelled_task_ids == []
            assert first.exit_code == -1

    asyncio.run(run())
    asyncio.run(engine.dispose())
