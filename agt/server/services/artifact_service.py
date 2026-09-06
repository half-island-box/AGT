"""Artifact registry service (Phase 2 artifact foundation).

An artifact is an *explicitly declared* Step deliverable. This service owns
the registry of record: registration (from daemon reports), metadata
queries, content materialization (inline <4KB vs server-disk blob), lazy
fetch coordination, and blob GC.

Design invariants (docs/impl/phase2-artifact-foundation.md):

- Registration happens for ALL topologies; byte transfer only crosses
  daemon boundaries. A same-daemon artifact is ``ready`` immediately with
  its producer-local path and its bytes never leave the daemon.
- The public reference is always ``agt-artifact://<artifact_id>``;
  ``local_path`` is internal transport detail.
- ``status``: ``registered`` (metadata only) → ``ready`` (bytes available:
  inline, server disk, or producer-local) → ``lost`` (producer's local
  file gone). Over-budget or daemon-offline situations surface explicit
  errors — never silent degradation.
- Scan/registration problems must never fail the Step report itself:
  invalid items are skipped with a warning, not raised.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Union

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import (
    INLINE_MAX_BYTES,
    MAX_ARTIFACTS_PER_STEP,
    REPORT_INLINE_BUDGET_BYTES,
    make_artifact_uri,
)
from ..models import ArtifactModel, TaskModel

if TYPE_CHECKING:
    from ..schemas import ArtifactReportItem

log = logging.getLogger(__name__)

FetchHook = Callable[[ArtifactModel], Awaitable[Union[bytes, "ArtifactFetchError"]]]


class ArtifactFetchError(Exception):
    """Lazy fetch could not bring back bytes."""

    def __init__(self, reason: str, message: str = "") -> None:
        self.reason = reason  # "producer_offline" | "artifact_missing"
        self.message = message or reason
        super().__init__(self.message)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# ── Per-artifact in-flight fetch dedup (design D6) ──────────────────
# One lock per artifact_id so concurrent consumers share a single
# producer round-trip. The dict is bounded: a stale lock is harmless, it
# just serializes two fetches.
_FETCH_LOCKS: dict[str, asyncio.Lock] = {}
_FETCH_LOCKS_MAX = 1024


def _fetch_lock(artifact_id: str) -> asyncio.Lock:
    lock = _FETCH_LOCKS.get(artifact_id)
    if lock is None:
        if len(_FETCH_LOCKS) >= _FETCH_LOCKS_MAX:
            _FETCH_LOCKS.clear()
        lock = asyncio.Lock()
        _FETCH_LOCKS[artifact_id] = lock
    return lock


# ── Name / budget validation ────────────────────────────────────────


def _valid_artifact_name(name: str) -> bool:
    """A public artifact name is a path *inside* ``.agt/outputs/``.

    Rejects absolute paths, ``..`` segments, and empty names. Forward
    slashes are allowed (subdirectories inside outputs/); the daemon has
    already enforced realpath containment — this is the server-side
    second line of defense.
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    parts = name.replace("\\", "/").split("/")
    return all(p not in ("", ".", "..") for p in parts)


def _decode_inline(item: "ArtifactReportItem", *, used_budget: int) -> tuple[bytes | None, int]:
    """Decode inline content under the per-report budget.

    Returns ``(decoded_bytes | None, new_budget_used)``. Over-budget or
    undecodable inline content drops to metadata-only (the artifact stays
    registered/ready via ``local_path``); it never fails the report.
    """
    raw = item.inline_b64
    if not raw:
        return None, used_budget
    try:
        data = base64.b64decode(raw, validate=True)
    except Exception:
        log.warning("artifact %r: invalid inline base64; storing metadata only", item.name)
        return None, used_budget
    if len(data) > INLINE_MAX_BYTES or used_budget + len(data) > REPORT_INLINE_BUDGET_BYTES:
        log.warning(
            "artifact %r: inline content exceeds budget (%d B); storing metadata only",
            item.name,
            len(data),
        )
        return None, used_budget
    return data, used_budget + len(data)


# ── Registration (report path) ──────────────────────────────────────


async def register_step_artifacts(
    db: AsyncSession,
    *,
    task: TaskModel,
    daemon_id: str,
    items: list["ArtifactReportItem"],
) -> list[str]:
    """Persist artifact rows reported for one Step.

    Called from the shared report entry point AFTER ownership was checked
    (``task.daemon_id == daemon_id``). Best-effort per item: invalid names,
    over-limit counts, and bad inline payloads are skipped with warnings —
    a scan problem must never fail the Step report. Returns the ids of the
    rows actually created.
    """
    if not items:
        return []

    if task.job_id is None:
        # Legacy rows never got a Job — nowhere to hang lineage from.
        log.warning("task %s has no job_id; dropping %d artifact(s)", task.task_id, len(items))
        return []

    now = _now()
    created: list[str] = []
    inline_budget_used = 0

    # Idempotency: eager uploads (POST /api/artifacts) may have already
    # registered rows for this Step before the report arrived. Matching
    # (name, sha256) pairs return the existing id instead of duplicating.
    existing_rows = (
        await db.execute(
            select(ArtifactModel).where(ArtifactModel.step_id == task.task_id)
        )
    ).scalars().all()
    existing_keys = {(r.name, r.sha256): r.artifact_id for r in existing_rows}

    for index, item in enumerate(items):
        if index >= MAX_ARTIFACTS_PER_STEP:
            log.warning(
                "task %s: artifact count exceeds %d; dropping the rest",
                task.task_id,
                MAX_ARTIFACTS_PER_STEP,
            )
            break
        if not _valid_artifact_name(item.name):
            log.warning("task %s: rejecting artifact name %r", task.task_id, item.name)
            continue
        prior_id = existing_keys.get((item.name, item.sha256))
        if prior_id is not None:
            created.append(prior_id)
            continue

        inline, inline_budget_used = _decode_inline(item, used_budget=inline_budget_used)

        # Status: inline content and producer-local paths both count as
        # "ready" — bytes are available (in the DB, or on the origin
        # daemon for same-daemon zero-copy consumption). Metadata-only
        # rows stay "registered" until a fetch materializes them.
        status = "ready" if (inline is not None or item.local_path) else "registered"

        artifact_id = str(uuid.uuid4())
        db.add(
            ArtifactModel(
                artifact_id=artifact_id,
                job_id=task.job_id,
                step_id=task.task_id,
                producer_identity_id=task.identity_did,
                name=item.name,
                mime_type=item.mime_type or "",
                summary=item.summary,
                status_flag=item.status_flag,
                size=item.size,
                sha256=item.sha256,
                inline_bytes=inline,
                local_path=item.local_path,
                origin_daemon_id=daemon_id,
                status=status,
                created_at=now,
            )
        )
        created.append(artifact_id)

    if created:
        await db.flush()
    return created


# ── Queries ─────────────────────────────────────────────────────────


async def get_artifact(db: AsyncSession, artifact_id: str) -> ArtifactModel | None:
    return await db.get(ArtifactModel, artifact_id)


async def artifacts_for_steps(
    db: AsyncSession, step_ids: list[str]
) -> dict[str, list[ArtifactModel]]:
    """Batch-fetch artifacts keyed by step_id (for status payloads)."""
    if not step_ids:
        return {}
    rows = (
        await db.execute(select(ArtifactModel).where(ArtifactModel.step_id.in_(step_ids)))
    ).scalars().all()
    by_step: dict[str, list[ArtifactModel]] = {}
    for row in rows:
        by_step.setdefault(row.step_id, []).append(row)
    return by_step


async def artifacts_for_job(db: AsyncSession, job_id: str) -> list[ArtifactModel]:
    """All artifacts produced by a Job (across its Steps).

    Backs the CLI ``agt artifact list --session`` / ``--job`` view — a
    session resolves to one Job, and every Step under it contributes its
    registered deliverables.
    """
    rows = (
        await db.execute(select(ArtifactModel).where(ArtifactModel.job_id == job_id))
    ).scalars().all()
    return list(rows)


async def artifacts_for_daemon(db: AsyncSession, daemon_id: str) -> list[dict]:
    """Every artifact a daemon produced, each annotated with ``session_id``.

    Backs ``agt artifact list --daemon X`` (no ``--session``) — the
    cross-session view. ``origin_daemon_id`` is the claim-time daemon that
    reported the artifact, so this is "what did this daemon actually
    produce". ``session_id`` comes from the producing Step's
    ``TaskModel.session_id`` (the same source ``list_daemon_sessions``
    uses).
    """
    rows = (
        await db.execute(
            select(ArtifactModel, TaskModel.session_id)
            .join(TaskModel, ArtifactModel.step_id == TaskModel.task_id)
            .where(ArtifactModel.origin_daemon_id == daemon_id)
            .order_by(ArtifactModel.created_at.desc())
        )
    ).all()
    out: list[dict] = []
    for artifact, session_id in rows:
        item = artifact_to_out(artifact)
        item["session_id"] = session_id
        out.append(item)
    return out


def artifact_to_out(row: ArtifactModel) -> dict:
    """Public metadata dict. NEVER includes ``local_path``."""
    return {
        "artifact_id": row.artifact_id,
        "job_id": row.job_id,
        "step_id": row.step_id,
        "producer_identity_id": row.producer_identity_id,
        "name": row.name,
        "mime_type": row.mime_type,
        "summary": row.summary,
        "status_flag": row.status_flag,
        "size": row.size,
        "sha256": row.sha256,
        "status": row.status,
        "uri": make_artifact_uri(row.artifact_id),
        "created_at": row.created_at,
    }


# ── Content materialization & resolution ────────────────────────────


def _verify_sha256(content: bytes, expected: str) -> None:
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected:
        raise ValueError({
            "type": "artifact_hash_mismatch",
            "message": f"expected {expected}, got {digest}",
        })


def materialize_content(
    artifact: ArtifactModel,
    content: bytes,
    artifacts_dir: Path,
    *,
    verify: bool = True,
) -> None:
    """Store fetched/uploaded bytes on the artifact row.

    <4KB goes inline into the DB; larger content is written to
    ``artifacts_dir/<artifact_id>`` (caller creates the dir). ``verify``
    checks the sha256 before storing — lazy fetches and eager uploads
    both verify.
    """
    if verify:
        _verify_sha256(content, artifact.sha256)
    if len(content) <= INLINE_MAX_BYTES:
        artifact.inline_bytes = content
    else:
        artifacts_dir.mkdir(parents=True, exist_ok=True)
        blob = artifacts_dir / artifact.artifact_id
        blob.write_bytes(content)
        artifact.storage_path = str(blob)
    artifact.status = "ready"


async def resolve_artifact_content(
    db: AsyncSession,
    artifact: ArtifactModel,
    *,
    artifacts_dir: Path,
    fetch_hook: FetchHook | None = None,
) -> tuple[bytes | None, Path | None]:
    """Resolve an artifact's bytes for a consumer.

    Returns ``(content_bytes, None)`` when the bytes are server-side
    (inline or blob). Same-daemon zero-copy is handled by the
    daemon-facing ``artifacts.get`` RPC which inspects ``local_path``
    directly — this path is for cross-daemon / API consumers.

    When only a producer-local path exists, triggers the lazy fetch via
    ``fetch_hook`` (server→origin-daemon RPC) under a per-artifact lock,
    verifies sha256, materializes, and returns the bytes. Raises
    ``ArtifactFetchError`` (producer_offline / artifact_missing) when the
    fetch cannot complete; a missing producer file marks the row ``lost``.
    """
    if artifact.status == "lost":
        raise ArtifactFetchError("artifact_missing", "artifact is lost")
    if artifact.inline_bytes is not None:
        return artifact.inline_bytes, None
    if artifact.storage_path:
        blob = Path(artifact.storage_path)
        if blob.exists():
            return blob.read_bytes(), None
        # Blob vanished (external cleanup) — fall through to refetch if
        # the producer path is still known.

    if not artifact.local_path:
        raise ArtifactFetchError("artifact_missing", "artifact has no materialized content")
    if fetch_hook is None:
        raise ArtifactFetchError(
            "producer_offline", "artifact not materialized and no fetch channel"
        )

    async with _fetch_lock(artifact.artifact_id):
        # Another consumer may have materialized it while we waited.
        await db.refresh(artifact)
        if artifact.inline_bytes is not None:
            return artifact.inline_bytes, None
        if artifact.storage_path and Path(artifact.storage_path).exists():
            return Path(artifact.storage_path).read_bytes(), None

        result = await fetch_hook(artifact)
        if isinstance(result, ArtifactFetchError):
            if result.reason == "artifact_missing":
                artifact.status = "lost"
            raise result
        _verify_sha256(result, artifact.sha256)
        materialize_content(artifact, result, artifacts_dir, verify=False)
        await db.commit()
        return result, None


# ── Lifecycle / GC ──────────────────────────────────────────────────


async def delete_artifacts_for_jobs(
    db: AsyncSession, job_ids: list[str], artifacts_dir: Path | None = None
) -> int:
    """Delete artifact rows for the given Jobs, GC'ing unreferenced blobs.

    Rows cascade via FK when a Job is deleted; this helper is for explicit
    cleanups (``agt task clean`` server path). Blob GC is reference-counted:
    a blob file is unlinked only when no surviving row shares its
    ``storage_path``.
    """
    if not job_ids:
        return 0
    rows = (
        await db.execute(select(ArtifactModel).where(ArtifactModel.job_id.in_(job_ids)))
    ).scalars().all()
    if not rows:
        return 0

    for row in rows:
        if row.storage_path and artifacts_dir is not None:
            siblings = (
                await db.execute(
                    select(func.count())
                    .select_from(ArtifactModel)
                    .where(
                        ArtifactModel.storage_path == row.storage_path,
                        ArtifactModel.artifact_id != row.artifact_id,
                    )
                )
            ).scalar_one()
            if siblings == 0:
                try:
                    Path(row.storage_path).unlink(missing_ok=True)
                except OSError as exc:
                    log.warning("blob GC failed for %s: %s", row.storage_path, exc)
        await db.delete(row)
    await db.flush()
    return len(rows)


__all__ = [
    "ArtifactFetchError",
    "register_step_artifacts",
    "get_artifact",
    "artifacts_for_steps",
    "artifacts_for_job",
    "artifacts_for_daemon",
    "artifact_to_out",
    "materialize_content",
    "resolve_artifact_content",
    "delete_artifacts_for_jobs",
]
