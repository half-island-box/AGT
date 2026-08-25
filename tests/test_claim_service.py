"""Phase 4 Step 4 + Option D — ``claim_tasks_for_daemon`` identity rendering.

When a task with ``identity_did`` is claimed, the server reads the
current IdentityModel and inlines the rendered AGENTS.md body into the
returned ``TaskPayload`` as ``identity_md``. The daemon writes this
verbatim into ``work_dir/IDENTITY.md`` — it never has to fetch from
the Server (impossible in reverse-dial).

These tests cover:

1. **identity_md is rendered when identity exists** — server reads the
   current IdentityModel state, produces the same markdown the user
   would see if they ran ``agt identity create`` on this machine.
2. **identity_md is None when task has no identity** — legacy tasks
   unaffected.
3. **identity_md is None when identity was deleted after submit** —
   claim is best-effort; a missing identity shouldn't fail the claim
   (matches pre-Option-D silent-skip semantics).
4. **Freshness** — an updated IdentityModel is reflected in the next
   claim's payload (no daemon-side cache to invalidate).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker

from agt.server.config import ServerConfig
from agt.server.database import make_engine, make_session_factory
from agt.server.main import create_app
from agt.server.migrate_schema import ensure_schema, migrate_identities_and_sessions
from agt.server.models import Base, RuntimeModel, TaskModel
from agt.server.services.binding_service import create_binding
from agt.server.services.claim_service import claim_tasks_for_daemon
from agt.server.services.identity_service import (
    _render_agent_md,
    create_identity,
    update_identity,
)


SECRET = "claim-service-test-secret"


def _build_engine_and_factory(tmp_path: Path) -> tuple[object, async_sessionmaker, object]:
    """Build the engine + session factory used by both the service and
    (optionally) the app. Tests seed via this factory so they share the
    same SQLite file as the rest of the system under test."""
    cfg = ServerConfig(
        host="127.0.0.1", port=8420,
        db_path=str(tmp_path / "server.db"), secret=SECRET,
        sessions_json_path=str(tmp_path / "no-sessions.json"),
    )
    engine = make_engine(cfg)
    factory = make_session_factory(engine)

    async def _setup() -> None:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
            await conn.run_sync(
                migrate_identities_and_sessions, tmp_path
            )

    asyncio.run(_setup())
    return engine, factory, cfg


async def _seed_identity_with_binding(
    factory, *, slug: str, daemon_id: str, provider: str,
    root_dir: Path, name: str | None = None,
) -> str:
    did = f"did:agt:{slug}"
    async with factory() as session:
        await create_identity(
            {
                "daemon_id": daemon_id,
                "name": name or slug.replace("-", " ").title(),
                "slug": slug,
                "description": f"I am {slug}.",
                "capabilities": ["python"],
            },
            session, root_dir,
        )
        await create_binding(
            did,
            {"daemon_id": daemon_id, "provider": provider,
             "priority": 0, "is_default": True},
            session,
        )
        await session.commit()
    return did


async def _seed_runtime(factory, *, daemon_id: str, provider: str) -> None:
    async with factory() as session:
        session.add(RuntimeModel(
            daemon_id=daemon_id, provider=provider,
            name=f"{provider} ({daemon_id})", version="1.0",
            status="online", device_name="dev",
            last_seen=datetime.now(timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
        ))
        await session.commit()


async def _submit_task(factory, *, identity_did: str | None) -> str:
    async with factory() as session:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        task = TaskModel(
            task_id="t1", provider="claude",
            prompt="hi", status="pending",
            work_dir=None, agent_cwd=None,
            identity_did=identity_did,
            created_at=now, updated_at=now,
        )
        session.add(task)
        await session.commit()
    return "t1"


# ── identity_md rendered at claim ──────────────────────────────────


class TestIdentityMdRendering:
    def test_payload_includes_rendered_md_when_identity_exists(
        self, tmp_path: Path,
    ) -> None:
        async def _seed() -> None:
            await _seed_identity_with_binding(
                factory, slug="reviewer", daemon_id="d1",
                provider="claude", root_dir=tmp_path,
            )

        engine, factory, _cfg = _build_engine_and_factory(tmp_path)
        try:
            asyncio.run(_seed())
            asyncio.run(_submit_task(factory, identity_did="did:agt:reviewer"))

            async def _claim() -> list:
                async with factory() as session:
                    return await claim_tasks_for_daemon(
                        session,
                        daemon_id="d1", max_tasks=1,
                        timeout_seconds=1,
                        runtime_ids=["claude"],
                    )

            results = asyncio.run(_claim())
            assert len(results) == 1
            payload = results[0]
            assert payload.identity_did == "did:agt:reviewer"
            assert payload.identity_md is not None
            # The rendered body includes the DID + name + the auto-gen banner.
            assert "did:agt:reviewer" in payload.identity_md
            assert "Reviewer" in payload.identity_md
            assert payload.identity_md.startswith("<!-- Auto-generated by AGT.")
        finally:
            asyncio.run(engine.dispose())

    def test_payload_identity_md_is_none_when_no_identity(
        self, tmp_path: Path,
    ) -> None:
        engine, factory, _cfg = _build_engine_and_factory(tmp_path)
        try:
            asyncio.run(_submit_task(factory, identity_did=None))

            async def _claim() -> list:
                async with factory() as session:
                    return await claim_tasks_for_daemon(
                        session,
                        daemon_id="d1", max_tasks=1,
                        timeout_seconds=1,
                        runtime_ids=["claude"],
                    )

            results = asyncio.run(_claim())
            assert len(results) == 1
            assert results[0].identity_did is None
            assert results[0].identity_md is None
        finally:
            asyncio.run(engine.dispose())

    def test_payload_identity_md_is_none_when_identity_deleted(
        self, tmp_path: Path,
    ) -> None:
        """Identity was deleted after the task was submitted. Claim
        should still succeed (best-effort); ``identity_md`` falls back
        to ``None`` so the daemon's inject silently no-ops, matching
        the pre-Option-D silent-skip behavior.
        """
        from agt.server.services.identity_service import delete_identity

        async def _seed_and_break() -> None:
            await _seed_identity_with_binding(
                factory, slug="ghost", daemon_id="d1",
                provider="claude", root_dir=tmp_path,
            )
            # Drop the identity but leave the task pointing at it.
            async with factory() as session:
                await delete_identity(
                    "did:agt:ghost", session, root_dir=tmp_path,
                )
                await session.commit()

        engine, factory, _cfg = _build_engine_and_factory(tmp_path)
        try:
            asyncio.run(_seed_and_break())
            asyncio.run(_submit_task(factory, identity_did="did:agt:ghost"))

            async def _claim() -> list:
                async with factory() as session:
                    return await claim_tasks_for_daemon(
                        session,
                        daemon_id="d1", max_tasks=1,
                        timeout_seconds=1,
                        runtime_ids=["claude"],
                    )

            results = asyncio.run(_claim())
            assert len(results) == 1
            # DID still on the payload (it was on the task), but no MD.
            assert results[0].identity_did == "did:agt:ghost"
            assert results[0].identity_md is None
        finally:
            asyncio.run(engine.dispose())

    def test_payload_identity_md_reflects_latest_update(
        self, tmp_path: Path,
    ) -> None:
        """An identity update must be visible in the next claim's
        payload — there's no daemon-side cache to invalidate.
        """
        async def _seed() -> None:
            await _seed_identity_with_binding(
                factory, slug="dynamic", daemon_id="d1",
                provider="claude", root_dir=tmp_path,
            )

        engine, factory, _cfg = _build_engine_and_factory(tmp_path)
        try:
            asyncio.run(_seed())
            asyncio.run(_submit_task(factory, identity_did="did:agt:dynamic"))

            async def _claim() -> str | None:
                async with factory() as session:
                    results = await claim_tasks_for_daemon(
                        session,
                        daemon_id="d1", max_tasks=1,
                        timeout_seconds=1,
                        runtime_ids=["claude"],
                    )
                return results[0].identity_md if results else None

            # First claim: original body.
            first = asyncio.run(_claim())
            assert first is not None
            assert "I am dynamic." in first

            # Update the identity description. The daemon never had
            # anything to invalidate — the next claim reads fresh.
            async def _update_and_requeue() -> None:
                async with factory() as session:
                    await update_identity(
                        "did:agt:dynamic",
                        {"name": "Dynamic", "description": "UPDATED DESC"},
                        session,
                    )
                    await session.commit()
                # Re-queue a fresh task so we can claim again.
                async with factory() as session:
                    now = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S"
                    )
                    session.add(TaskModel(
                        task_id="t2", provider="claude",
                        prompt="again", status="pending",
                        work_dir=None, agent_cwd=None,
                        identity_did="did:agt:dynamic",
                        created_at=now, updated_at=now,
                    ))
                    await session.commit()

            asyncio.run(_update_and_requeue())
            second = asyncio.run(_claim())
            assert second is not None
            assert "UPDATED DESC" in second
            assert "I am dynamic." not in second
        finally:
            asyncio.run(engine.dispose())

    def test_render_helper_produces_expected_format(
        self, tmp_path: Path,
    ) -> None:
        """Sanity: ``_render_agent_md`` is what we inline — its output
        shape must be the same body that ``agt identity create`` writes
        to disk so the daemon's IDENTITY.md is byte-equivalent."""
        ident = {
            "did": "did:agt:smoke",
            "daemon_id": "d1",
            "name": "Smoke",
            "description": "smoke test",
            "capabilities": ["a", "b"],
            "status": "active",
        }
        body = _render_agent_md(ident)
        assert body.startswith("<!-- Auto-generated by AGT.")
        assert "# Smoke" in body
        assert "did:agt:smoke" in body
        assert "- a" in body
        assert "- b" in body


# Silence the unused import — the test only needs the engine dispose.
_ = create_app  # keep the import surface stable for some IDE checkers