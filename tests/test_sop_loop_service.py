"""SOP loop execution tests.

Verifies that declared ``loops:`` blocks materialise per-iteration jobs
and advance through iterations until ``max_iterations`` is reached.
"""

from __future__ import annotations

import asyncio

from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.database import make_engine
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, JobModel, RuntimeModel
from agt.server.services.sop_loop_service import check_loop_advance
from agt.server.services.sop_service import expand_sop
from agt.sop.dsl import parse_sop


SECRET = "sop-loop-test-secret"

SPEC_YAML = """
name: review-fix-loop
loops:
  - name: refine
    nodes: [review, fix]
    max_iterations: 3
tasks:
  - name: review
    provider: claude
    input: Review code
    output: review-report
  - name: fix
    provider: claude
    input: Apply ${review.review-report}
    depends_on: [review]
    output: fix-result
"""


async def _setup_db(tmp_path):
    from agt.server.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "loop.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_schema)
    return engine, cfg


async def _register_runtime(db: AsyncSession, daemon_id: str = "local-test") -> None:
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.add(
        RuntimeModel(
            daemon_id=daemon_id,
            provider="claude",
            name="claude",
            version="1.0",
            device_name="test-device",
            last_seen=now,
        )
    )
    await db.commit()


async def _loop_jobs(db: AsyncSession, root_id: str, loop_name: str):
    from sqlalchemy import select

    rows = (
        await db.execute(
            select(JobModel).where(
                JobModel.root_job_id == root_id,
                JobModel.sop_loop_name == loop_name,
            )
        )
    ).scalars().all()
    return rows


def _set_terminal(job: JobModel) -> None:
    """Shortcut to mark a job done without running a subprocess."""
    job.status = "done"


def test_loop_expansion_and_advance(tmp_path) -> None:
    engine, _cfg = asyncio.run(_setup_db(tmp_path))

    async def _run() -> None:
        async with AsyncSession(engine) as db:
            await _register_runtime(db)
            spec = parse_sop(SPEC_YAML)
            expansion = await expand_sop(db, spec)
            await db.commit()
            root_id = expansion.root_job_id

            # Iteration 1 should exist; sentinel should be blocked.
            jobs = await _loop_jobs(db, root_id, "refine")
            sentinel = [j for j in jobs if j.sop_iteration == 0]
            iter1 = [j for j in jobs if j.sop_iteration == 1]
            assert len(sentinel) == 1
            assert sentinel[0].status == "blocked"
            assert len(iter1) == 2

            # Advance through iterations.
            for iteration in range(1, 3):
                current = [j for j in await _loop_jobs(db, root_id, "refine") if j.sop_iteration == iteration]
                assert len(current) == 2
                for job in current:
                    _set_terminal(job)
                await db.commit()
                await check_loop_advance(db, root_id)
                await db.commit()

                next_jobs = [j for j in await _loop_jobs(db, root_id, "refine") if j.sop_iteration == iteration + 1]
                assert len(next_jobs) == 2, f"expected iteration {iteration + 1} jobs"

            # Final iteration: mark done, sentinel should complete.
            final = [j for j in await _loop_jobs(db, root_id, "refine") if j.sop_iteration == 3]
            for job in final:
                _set_terminal(job)
            await db.commit()
            await check_loop_advance(db, root_id)
            await db.commit()

            sentinel_job = (await _loop_jobs(db, root_id, "refine"))[0]
            # Re-fetch sentinel (it was the first row above, but safer to filter).
            sentinel_job = [j for j in await _loop_jobs(db, root_id, "refine") if j.sop_iteration == 0][0]
            assert sentinel_job.status == "done"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())
