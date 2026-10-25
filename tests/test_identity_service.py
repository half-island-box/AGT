"""Tests for the identity service layer (Phase 4 Step 1).

Covers:
- CRUD: create / get / list / update / deprecate / revoke / delete
- DID generation: slug validation, derivation from name, duplicates
- Status transitions: active → deprecated → revoked
- AGENTS.md: write-once idempotency, force overwrite via sync, content
- _dir_name: slug extraction + hash fallback for malformed DIDs

Follows the repo's existing async test convention: sync test functions
driving async code via ``asyncio.run`` (see test_target_routing.py).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from agt.server.database import make_session_factory
from agt.server.models import Base, StepModel
from agt.server.services.identity_service import (
    _dir_name,
    _render_agent_md,
    create_identity,
    delete_identity,
    deprecate_identity,
    get_identity,
    list_identities,
    normalize_identity_did,
    revoke_identity,
    sync_agent_md,
    update_identity,
    write_agent_md,
)


class _DB:
    """Per-test async session factory over a tmp SQLite file.

    NullPool: each ``db.run`` drives its own event loop (asyncio.run), and
    pooled aiosqlite connections cannot be reused across loops.
    """

    def __init__(self, tmp_path: Path) -> None:
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{tmp_path}/test.db", poolclass=NullPool
        )
        self.factory = make_session_factory(self.engine)

    def run(self, coro_fn, *args, **kwargs):
        """Run one async service call in its own session, return result."""
        async def _inner():
            async with self.factory() as session:
                return await coro_fn(*args, **kwargs)
        return asyncio.run(_inner())

    def session(self):
        """Context manager yielding a session for raw ORM work."""
        return self.factory()

    def dispose(self) -> None:
        asyncio.run(self.engine.dispose())


@pytest.fixture
def db(tmp_path: Path) -> _DB:
    inst = _DB(tmp_path)
    asyncio.run(_create_all(inst.engine))
    yield inst
    inst.dispose()


async def _create_all(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _payload(**overrides) -> dict:
    base = {
        "daemon_id": "daemon-A",
        "name": "Code Reviewer",
        "slug": "code-reviewer",
        "description": "Python code review",
        "capabilities": ["code-review", "doc-style-audit"],
    }
    base.update(overrides)
    return base


# ── DID generation & _dir_name (pure sync) ──────────────────────────


class TestDirName:
    def test_extracts_slug_from_valid_did(self) -> None:
        assert _dir_name("did:agt:code-reviewer") == "code-reviewer"

    def test_malformed_did_falls_back_to_hash(self) -> None:
        # Wrong arity (3-segment DID is the only legal shape now) → hash.
        name = _dir_name("did:agt:daemon-A:code-reviewer")
        assert name.startswith("identity-")
        assert len(name) == len("identity-") + 8

        name = _dir_name("not-a-did")
        assert name.startswith("identity-")

    def test_invalid_slug_falls_back_to_slug_hash(self) -> None:
        name = _dir_name("did:agt:UPPER")
        assert name.startswith("UPPER-")
        assert len(name) == len("UPPER-") + 8


class TestNormalizeIdentityDid:
    """``normalize_identity_did`` turns user input into a canonical DID."""

    def test_full_did_passthrough(self) -> None:
        assert normalize_identity_did("did:agt:code-reviewer") == "did:agt:code-reviewer"

    def test_strips_surrounding_whitespace(self) -> None:
        assert normalize_identity_did("  did:agt:code-reviewer  ") == "did:agt:code-reviewer"

    def test_bare_slug_prefixed(self) -> None:
        assert normalize_identity_did("code-reviewer") == "did:agt:code-reviewer"

    def test_typo_scheme_rejected(self) -> None:
        # The common "id:agt:..." typo must not fall through to a 404.
        with pytest.raises(ValueError, match="unknown scheme 'id:'"):
            normalize_identity_did("id:agt:code-reviewer")

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not be empty"):
            normalize_identity_did("")
        with pytest.raises(ValueError, match="must not be empty"):
            normalize_identity_did("   ")

    def test_invalid_slug_after_prefix_rejected(self) -> None:
        with pytest.raises(ValueError, match="after 'did:agt:'"):
            normalize_identity_did("did:agt:UPPER")

    def test_invalid_bare_slug_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid identity slug"):
            normalize_identity_did("Bad_Slug!")


class TestDIDGeneration:
    def test_create_generates_expected_did(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        assert did == "did:agt:code-reviewer"

    def test_slug_derived_from_name_when_missing(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload(slug=None, name="Doc Translator"))
        assert did == "did:agt:doc-translator"

    def test_invalid_slug_rejected(self, db: _DB, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid slug"):
            db.run(_create_via, db, tmp_path, _payload(slug="Bad_Slug!"))

    def test_leading_hyphen_slug_rejected(self, db: _DB, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="Invalid slug"):
            db.run(_create_via, db, tmp_path, _payload(slug="-lead"))

    def test_duplicate_rejected(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        with pytest.raises(ValueError, match="already exists"):
            db.run(_create_via, db, tmp_path, _payload())

    def test_same_slug_different_daemon_globally_rejected(self, db: _DB, tmp_path: Path) -> None:
        """Namespace isolation removed in 2026-08: the same slug is now a
        global conflict regardless of which daemon created it."""
        db.run(_create_via, db, tmp_path, _payload())
        with pytest.raises(ValueError, match="already exists"):
            db.run(_create_via, db, tmp_path, _payload(daemon_id="daemon-B"))


async def _create_via(db: _DB, tmp_path: Path, payload: dict) -> str:
    """Open a session on ``db`` and run create_identity inside it."""
    async with db.session() as session:
        return await create_identity(payload, session, tmp_path)


async def _get_via(db: _DB, did: str):
    async with db.session() as session:
        return await get_identity(did, session)


async def _list_via(db: _DB, **filters):
    async with db.session() as session:
        return await list_identities(db=session, **filters)


async def _update_via(db: _DB, did: str, payload: dict):
    async with db.session() as session:
        return await update_identity(did, payload, session)


async def _deprecate_via(db: _DB, did: str):
    async with db.session() as session:
        return await deprecate_identity(did, session)


async def _revoke_via(db: _DB, did: str):
    async with db.session() as session:
        return await revoke_identity(did, session)


async def _delete_via(db: _DB, did: str, root_dir: Path | None = None):
    async with db.session() as session:
        return await delete_identity(did, session, root_dir=root_dir)


async def _sync_via(db: _DB, did: str, tmp_path: Path):
    async with db.session() as session:
        return await sync_agent_md(did, session, tmp_path)


async def _add_task(db: _DB, task_id: str, identity_did: str) -> None:
    async with db.session() as session:
        session.add(StepModel(
            step_id=task_id, provider="claude", prompt="hi",
            status="completed", created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00", identity_did=identity_did,
        ))
        await session.commit()


# ── CRUD ────────────────────────────────────────────────────────────


class TestCreateGet:
    def test_create_persists_all_fields(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        ident = db.run(_get_via, db, "did:agt:code-reviewer")
        assert ident is not None
        assert ident["name"] == "Code Reviewer"
        assert ident["status"] == "active"
        assert ident["capabilities"] == ["code-review", "doc-style-audit"]
        assert ident["daemon_id"] == "daemon-A"

    def test_get_missing_returns_none(self, db: _DB) -> None:
        assert db.run(_get_via, db, "did:agt:nope") is None


class TestList:
    def test_list_unfiltered(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        db.run(_create_via, db, tmp_path, _payload(slug="doc-translator", name="Doc"))
        idents = db.run(_list_via, db)
        assert len(idents) == 2

    def test_list_filters_by_daemon(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        # Namespace isolation removed 2026-08: must use a different slug now.
        db.run(_create_via, db, tmp_path, _payload(slug="reviewer-b", daemon_id="daemon-B"))
        idents = db.run(_list_via, db, daemon_id="daemon-A")
        assert len(idents) == 1
        assert idents[0]["daemon_id"] == "daemon-A"

    def test_list_filters_by_status(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        db.run(_create_via, db, tmp_path, _payload(slug="other", name="Other"))
        db.run(_deprecate_via, db, "did:agt:other")
        active = db.run(_list_via, db, status="active")
        assert len(active) == 1
        assert active[0]["did"].endswith("code-reviewer")


class TestUpdate:
    def test_partial_update_name_only(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        updated = db.run(_update_via, db, did, {"name": "Renamed"})
        assert updated["name"] == "Renamed"
        assert updated["description"] == "Python code review"  # unchanged

    def test_capability_add_and_remove(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        updated = db.run(
            _update_via, db, did,
            {"add_capabilities": ["refactor"], "remove_capabilities": ["doc-style-audit"]},
        )
        assert sorted(updated["capabilities"]) == ["code-review", "refactor"]

    def test_update_missing_returns_none(self, db: _DB) -> None:
        assert db.run(_update_via, db, "did:agt:nope", {"name": "x"}) is None

    def test_update_does_not_rewrite_agent_md(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        md_path = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        original = md_path.read_text()
        md_path.write_text(original + "\n<!-- manual note -->")
        db.run(_update_via, db, did, {"name": "Renamed"})
        assert "manual note" in md_path.read_text()


class TestStatusTransitions:
    def test_deprecate_sets_status(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        assert db.run(_deprecate_via, db, did)["status"] == "deprecated"

    def test_deprecate_idempotent(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        db.run(_deprecate_via, db, did)
        assert db.run(_deprecate_via, db, did)["status"] == "deprecated"

    def test_revoke_sets_status(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        assert db.run(_revoke_via, db, did)["status"] == "revoked"

    def test_deprecate_missing_returns_none(self, db: _DB) -> None:
        assert db.run(_deprecate_via, db, "did:agt:nope") is None

    def test_revoke_missing_returns_none(self, db: _DB) -> None:
        assert db.run(_revoke_via, db, "did:agt:nope") is None


class TestDelete:
    def test_delete_unused_identity(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        assert db.run(_delete_via, db, did, tmp_path) is True
        assert db.run(_get_via, db, did) is None

    def test_delete_missing_returns_false(self, db: _DB) -> None:
        assert db.run(_delete_via, db, "did:agt:nope") is False

    def test_delete_refused_when_task_references_exist(self, db: _DB, tmp_path: Path) -> None:
        did = db.run(_create_via, db, tmp_path, _payload())
        db.run(_add_task, db, "t-1", did)
        with pytest.raises(ValueError, match="task"):
            db.run(_delete_via, db, did, tmp_path)
        assert db.run(_get_via, db, did) is not None

    def test_delete_removes_identity_directory(self, db: _DB, tmp_path: Path) -> None:
        """Deleting an identity must rmtree its ~/.agt/identities/<slug>/ dir."""
        did = db.run(_create_via, db, tmp_path, _payload())
        ident_dir = tmp_path / ".agt" / "identities" / "code-reviewer"
        assert (ident_dir / "AGENTS.md").exists()  # created on identity create

        assert db.run(_delete_via, db, did, tmp_path) is True

        assert not ident_dir.exists()
        assert not (tmp_path / ".agt" / "identities").exists() or not any(
            (tmp_path / ".agt" / "identities").iterdir()
        )  # identities root left empty or removed

    def test_delete_no_root_skips_filesystem(self, db: _DB, tmp_path: Path) -> None:
        """root_dir=None (defensive default) removes DB row but not the dir."""
        did = db.run(_create_via, db, tmp_path, _payload())
        ident_dir = tmp_path / ".agt" / "identities" / "code-reviewer"
        assert ident_dir.exists()

        assert db.run(_delete_via, db, did) is True  # root_dir=None

        assert ident_dir.exists()  # file dir untouched
        assert db.run(_get_via, db, did) is None


# ── AGENTS.md files ──────────────────────────────────────────────────


class TestAgentMd:
    def test_created_on_identity_create(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        assert md.exists()
        content = md.read_text()
        assert "Code Reviewer" in content
        assert "did:agt:code-reviewer" in content
        assert "- code-review" in content

    def test_write_once_does_not_overwrite(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        md.write_text("MANUAL EDITS ONLY")
        ident = db.run(_get_via, db, "did:agt:code-reviewer")
        # Non-forced write must not clobber manual edits.
        write_agent_md(ident, tmp_path, force=False)
        assert md.read_text() == "MANUAL EDITS ONLY"

    def test_force_overwrite_via_sync(self, db: _DB, tmp_path: Path) -> None:
        db.run(_create_via, db, tmp_path, _payload())
        did = "did:agt:code-reviewer"
        md = tmp_path / ".agt" / "identities" / "code-reviewer" / "AGENTS.md"
        md.write_text("MANUAL EDITS ONLY")
        db.run(_sync_via, db, did, tmp_path)
        content = md.read_text()
        assert "MANUAL EDITS" not in content
        assert "Code Reviewer" in content

    def test_sync_missing_identity_returns_none(self, db: _DB, tmp_path: Path) -> None:
        assert db.run(_sync_via, db, "did:agt:nope", tmp_path) is None


class TestRenderAgentMd:
    def test_renders_from_dict(self) -> None:
        content = _render_agent_md({
            "did": "did:agt:x",
            "name": "X",
            "description": "desc",
            "capabilities": json.dumps(["a", "b"]),
            "status": "active",
            "daemon_id": "d",
        })
        assert "# X" in content
        assert "did:agt:x" in content
        assert "- a" in content
        assert "- b" in content

    def test_renders_empty_capabilities(self) -> None:
        content = _render_agent_md({
            "did": "did:agt:x",
            "name": "X",
            "description": "",
            "capabilities": "[]",
            "status": "active",
            "daemon_id": "d",
        })
        assert "No capabilities defined" in content
