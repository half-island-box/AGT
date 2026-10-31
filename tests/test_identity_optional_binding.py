"""Identity can run with a provider without a matching binding record."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.database import make_engine, make_session_factory
from agt.server.models import Base, JobModel, RuntimeModel
from agt.server.services.goal_service import submit_goal
from agt.server.services.identity_service import create_identity
from agt.server.services.sop_service import expand_sop
from agt.sop.dsl import parse_sop


SECRET = "identity-optional-binding-test-secret"
DID = "did:agt:reviewer"


async def _setup_db(tmp_path):
    from agt.server.config import ServerConfig

    cfg = ServerConfig(
        host="127.0.0.1",
        port=8420,
        db_path=str(tmp_path / "optional-binding.db"),
        secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, make_session_factory(engine)


async def _seed_identity(factory, tmp_path) -> None:
    async with factory() as db:
        await create_identity(
            {
                "daemon_id": "local",
                "name": "Reviewer",
                "slug": "reviewer",
            },
            db,
            tmp_path,
        )


async def _register_runtime(
    db: AsyncSession,
    *,
    daemon_id: str = "runtime-a",
    provider: str = "claude",
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    db.add(
        RuntimeModel(
            daemon_id=daemon_id,
            provider=provider,
            name=provider,
            version="1.0",
            device_name="test-device",
            last_seen=now,
        )
    )
    await db.flush()


def test_goal_submit_allows_identity_without_binding(tmp_path) -> None:
    engine, factory = asyncio.run(_setup_db(tmp_path))

    async def _run() -> None:
        await _seed_identity(factory, tmp_path)
        async with factory() as db:
            await _register_runtime(db)
            job = await submit_goal(
                db,
                goal="review this change",
                identity_did=DID,
                provider="claude",
            )
            await db.commit()
            assert job.identity_did == DID
            assert job.provider == "claude"
            assert job.target_daemon_id == "runtime-a"

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_sop_expands_identity_without_binding(tmp_path) -> None:
    engine, factory = asyncio.run(_setup_db(tmp_path))
    spec = parse_sop(
        """
name: review
identities:
  reviewer: {did: did:agt:reviewer, provider: claude}
tasks:
  - name: review
    identity: reviewer
    input: review this change
"""
    )

    async def _run() -> None:
        await _seed_identity(factory, tmp_path)
        async with factory() as db:
            await _register_runtime(db)
            expansion = await expand_sop(db, spec)
            await db.commit()
            root = await db.get(JobModel, expansion.root_job_id)
            assert root is not None
            assert root.identity_did == DID
            assert root.provider == "claude"
            assert root.target_daemon_id == "runtime-a"
            assert root.steps

    try:
        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())
