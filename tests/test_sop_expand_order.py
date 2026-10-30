"""SOP expansion ordering tests.

``expand_sop`` must not assume the author lists a dependent node after its
upstreams: parent edges are resolved against already-materialized jobs, so
a DAG whose ``tasks`` order puts a dependent first must expand without a
bare ``KeyError`` and wire the same parent edges.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.database import make_engine
from agt.server.migrate_schema import ensure_schema
from agt.server.models import Base, JobParentModel, RuntimeModel
from agt.server.services.sop_service import expand_sop
from agt.sop.dsl import parse_sop


SECRET = "sop-expand-order-test-secret"


def _setup_db(tmp_path):
    from agt.server.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "expand-order.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    return engine, cfg


async def _prepare(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(ensure_schema)


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


SPEC_DEPENDENT_FIRST = """
name: dependent-first
identities:
  worker: {provider: claude}
tasks:
  - name: synth
    identity: worker
    input: 综合 ${a.a-out} 与 ${b.b-out}
    depends_on: [a, b]
  - name: a
    identity: worker
    input: 产出 a
    output: a-out
  - name: b
    identity: worker
    input: 产出 b
    output: b-out
"""


SPEC_LOOP_DEPENDENT_FIRST = """
name: loop-dependent-first
loops:
  - name: refine
    nodes: [A, B, C]
    max_iterations: 2
    entry: A
tasks:
  - name: B
    provider: claude
    input: step b
    depends_on: [C]
  - name: A
    provider: claude
    input: step a
    depends_on: [B]
  - name: C
    provider: claude
    input: step c
"""


async def _parent_ids(db: AsyncSession, job_id: str) -> list[str]:
    rows = (
        await db.execute(
            select(JobParentModel.parent_job_id).where(JobParentModel.job_id == job_id)
        )
    ).scalars().all()
    return list(rows)


def test_non_loop_dependent_first(tmp_path) -> None:
    engine, _cfg = _setup_db(tmp_path)

    async def _run() -> None:
        await _prepare(engine)
        async with AsyncSession(engine) as db:
            await _register_runtime(db)
            spec = parse_sop(SPEC_DEPENDENT_FIRST)
            expansion = await expand_sop(db, spec)
            await db.commit()

            # synth depends on a and b even though it is listed first.
            synth_parents = await _parent_ids(db, expansion.node_jobs["synth"])
            assert set(synth_parents) == {
                expansion.node_jobs["a"],
                expansion.node_jobs["b"],
            }

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_loop_dependent_first(tmp_path) -> None:
    engine, _cfg = _setup_db(tmp_path)

    async def _run() -> None:
        await _prepare(engine)
        async with AsyncSession(engine) as db:
            await _register_runtime(db)
            spec = parse_sop(SPEC_LOOP_DEPENDENT_FIRST)
            expansion = await expand_sop(db, spec)
            await db.commit()

            # Same-loop internal edges resolve even when the upstream is
            # created after the dependent (and when the entry is the root).
            b_parents = await _parent_ids(db, expansion.loop_node_jobs["B"][1])
            assert b_parents == [expansion.loop_node_jobs["C"][1]]
            a_parents = await _parent_ids(db, expansion.loop_node_jobs["A"][1])
            assert a_parents == [expansion.loop_node_jobs["B"][1]]

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())
