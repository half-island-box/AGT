"""Shared claim logic used by both HTTP and WS paths.

Both ``POST /api/tasks/claim`` (HTTP) and the WS ``tasks.claim`` RPC
delegate here so the SELECT/UPDATE/transaction logic lives in exactly
one place. Two paths calling the same DB code is the natural place for
bugs to diverge — one service function = one behavior.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import DataPart, Message, message_from_prompt
from ..models import JobModel, SessionModel, StepModel
from ..schemas import TaskPayload
from .context_service import build_context_brief
from .identity_service import _render_agent_md, get_identity

log = logging.getLogger(__name__)

# Module-level event used to wake up long-polling HTTP claim requests as
# soon as a new Step is submitted. Best-effort: if the event fires
# spuriously, claimers simply re-query the DB and go back to sleep.
_step_available_event: asyncio.Event | None = None


def get_step_available_event() -> asyncio.Event:
    """Return the module-level step-available event, creating it lazily."""
    global _step_available_event
    if _step_available_event is None:
        _step_available_event = asyncio.Event()
    return _step_available_event


def notify_steps_available() -> None:
    """Wake any HTTP claim requests currently long-polling for Steps."""
    event = _step_available_event
    if event is not None:
        event.set()


async def claim_steps_for_daemon(
    db: AsyncSession,
    *,
    daemon_id: str,
    max_tasks: int,
    timeout_seconds: int,
    runtime_ids: list[str] | None = None,
) -> list[TaskPayload]:
    """Long-poll for up to ``max_tasks`` pending Steps for ``daemon_id``.

    Returns immediately when at least one matching Step is available.
    Otherwise waits up to ``timeout_seconds``, polling every 500 ms,
    and returns an empty list when the deadline passes.

    Provider routing (Step 3.4): when ``runtime_ids`` is provided, only
    Steps whose ``provider`` appears in the list are eligible. An empty
    ``runtime_ids`` list means "no providers declared" and yields no
    Steps (a daemon must declare what it can run before claiming).

    Identity rendering (Option D): when the claimed Step carries an
    ``identity_did``, we look up the IdentityModel and inline the
    rendered AGENTS.md body into the payload as ``identity_md``. The
    daemon writes this verbatim into ``work_dir/IDENTITY.md`` and never
    needs to call back to the Server — which is impossible in a
    reverse-dial setup where the daemon sits behind NAT. Rendering at
    claim time (instead of submit time / on a cached daemon file)
    guarantees that any identity update is picked up by the next claim
    within one poll interval (~500ms).
    """
    deadline = time.monotonic() + timeout_seconds
    poll_interval = 0.5
    event = get_step_available_event()

    # Step 3.4: explicit "I have nothing" short-circuits the poll. The
    # daemon declared no capabilities, so we honor that immediately
    # instead of burning the deadline in an empty loop.
    if runtime_ids is not None and not runtime_ids:
        return []

    while True:
        stmt = _build_select(daemon_id=daemon_id, max_tasks=max_tasks,
                             runtime_ids=runtime_ids)
        candidate = (await db.execute(stmt)).scalars().first()

        if candidate is not None:
            payload = await _claim_candidate(
                db, candidate=candidate, daemon_id=daemon_id,
            )
            await db.commit()
            return [payload]

        if time.monotonic() >= deadline:
            return []

        # Sleep until the next poll, but wake immediately when a Step is
        # submitted via ``notify_steps_available``.
        try:
            await asyncio.wait_for(event.wait(), timeout=poll_interval)
        except asyncio.TimeoutError:
            pass
        else:
            # Woken by a Step submission. Clear the event so future
            # claimers block again; concurrent claimers all race to claim,
            # which is harmless because ``_claim_candidate`` is atomic.
            if event.is_set():
                event.clear()
        await db.rollback()


async def cancelled_steps_for_daemon(db: AsyncSession, daemon_id: str) -> list[str]:
    """Unreported cancellations for THIS daemon (Phase 4 claim channel).

    Steps the daemon owns that were cancelled server-side and have not
    been reported since (``exit_code IS NULL``). The daemon kills those
    subprocesses and reports cancelled — the report sets ``exit_code``,
    which drops them from this list (self-draining, no ack state).
    """
    rows = (
        await db.execute(
            select(StepModel.step_id).where(
                StepModel.daemon_id == daemon_id,
                StepModel.status == "cancelled",
                StepModel.exit_code.is_(None),
            )
        )
    ).scalars().all()
    return list(rows)


def _build_select(
    *,
    daemon_id: str,
    max_tasks: int,
    runtime_ids: list[str] | None,
):
    """Build the long-poll SELECT statement for claimable Steps.

    Pulled out of ``claim_steps_for_daemon`` so the daemon-targeting /
    provider-filter / LIMIT logic is easy to read and the outer loop
    stays focused on polling.
    """
    from sqlalchemy import exists

    stmt = select(StepModel).where(
        StepModel.status == "pending",
        # Step 3.6: daemon targeting. Free Steps (target is NULL) are
        # claimable by anyone; targeted Steps are visible only to the
        # daemon they name. Both classes coexist in the same queue.
        or_(
            StepModel.target_daemon_id.is_(None),
            StepModel.target_daemon_id == daemon_id,
        ),
        # Phase 4 (review fix #8, double insurance): never hand out a
        # Step whose parent Job is already terminal — cancel_job flips
        # pending steps to cancelled, this guard catches any other race
        # that leaves an orphan pending step under a dead job.
        or_(
            StepModel.job_id.is_(None),
            ~exists().where(
                JobModel.job_id == StepModel.job_id,
                JobModel.status.in_(("done", "failed", "cancelled")),
            ),
        ),
    )
    # Step 3.4: provider-aware filter. Empty ``runtime_ids`` is handled
    # in the caller (early return). Here we only add the ``in_`` clause
    # when the daemon actually declared capabilities.
    if runtime_ids is not None:
        stmt = stmt.where(StepModel.provider.in_(runtime_ids))
    # Note: the SELECT/LIMIT is currently 1 — one Step per long-poll
    # call (long-poll semantics). max_tasks is reserved for a future
    # batch-claim path and is normalized here to keep the signature
    # stable.
    effective_limit = max(1, int(max_tasks))
    stmt = stmt.order_by(StepModel.created_at).limit(effective_limit)
    return stmt


async def _claim_candidate(
    db: AsyncSession,
    *,
    candidate: StepModel,
    daemon_id: str,
) -> TaskPayload:
    """Mark ``candidate`` running, look up its session + identity, build
    the payload that goes back to the daemon.

    The caller is responsible for ``db.commit()`` — keeping that outside
    lets tests stage the work and inspect intermediate state if needed.

    Phase 1: claiming a Step also transitions the parent Job to running.
    """
    candidate.status = "running"
    candidate.daemon_id = daemon_id
    candidate.updated_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # Drive the parent Job state machine.
    from .job_service import claim_step

    await claim_step(db, candidate.step_id)

    session_rec = None
    if candidate.job_id is not None:
        session_rec = (
            await db.execute(
                select(SessionModel).where(
                    SessionModel.job_id == candidate.job_id,
                    SessionModel.provider == candidate.provider,
                )
            )
        ).scalars().first()
    resume_session_id = session_rec.session_id if session_rec else candidate.session_id

    # Option D: render the identity's AGENTS.md here so the daemon can
    # write it directly without a callback to the Server. Defensive: a
    # missing identity (e.g. deleted after submit) shouldn't fail the
    # claim — the Step just runs without an injected identity, which
    # matches the pre-Option-D silent-skip semantics.
    identity_md = await _render_identity_md(db, candidate)

    message_dict, rendered_prompt = await _build_step_message(
        db, candidate, daemon_id=daemon_id
    )

    # Phase 3 (D9): server-computed outputs subdirectory — child jobs
    # isolate their whitelist dir per job_id; root jobs share the Phase 2
    # directory (None). The daemon consumes this without any lineage
    # knowledge of its own.
    outputs_subdir = None
    if candidate.job_id is not None:
        job = await db.get(JobModel, candidate.job_id)
        if job is not None and job.parent_job_id is not None:
            outputs_subdir = f".agt/outputs/{job.job_id}/"

    return TaskPayload(
        task_id=candidate.step_id,
        provider=candidate.provider,
        prompt=rendered_prompt,
        resume_session_id=resume_session_id,
        work_dir=None,
        identity_did=candidate.identity_did,
        identity_md=identity_md,
        agent_cwd=candidate.agent_cwd,
        message=message_dict,
        outputs_subdir=outputs_subdir,
    )


async def _build_step_message(
    db: AsyncSession,
    candidate: StepModel,
    *,
    daemon_id: str,
) -> tuple[dict | None, str]:
    """Materialize the Step's structured input for the claiming daemon.

    Returns ``(message_dict, rendered_prompt)``. ``message_dict`` is
    ``None`` for legacy rows without ``input_message`` (prompt used
    verbatim). FileParts gain resolution hints where cheap:

    - inline content (<4KB, from the artifact row) → ``inline_b64``;
    - the claiming daemon IS the producer → ``local_path`` (zero-copy,
      the daemon reads its own disk, no transfer at all).

    Large cross-daemon references carry only the URI — the daemon
    resolves them on demand via ``artifacts.get``.
    """
    from ...a2a import parse_artifact_uri, parse_upload_uri, render_message
    from ..models import ArtifactModel, InputUploadModel

    job = await db.get(JobModel, candidate.job_id) if candidate.job_id else None
    if candidate.input_message:
        message = Message.model_validate_json(candidate.input_message)
    elif job is not None and job.job_type == "goal":
        message = message_from_prompt(candidate.prompt)
    else:
        return None, candidate.prompt

    # Shared context is a claim-time projection. Keep the submitted
    # input_message immutable and audit the exact Brief on the Step.
    if job is not None and job.job_type == "goal":
        projected = await build_context_brief(db, step=candidate, job=job)
        if projected is not None:
            brief, context_file_parts = projected
            message.parts.append(DataPart(data={"context_brief": brief}))
            message.parts.extend(context_file_parts)
            candidate.context_brief_json = _canonical_context_json(brief)
            candidate.context_snapshot_id = str(brief["snapshot_id"])
            candidate.context_watermark = int(brief["watermark"])
    # Work on raw dicts so server-injected hint keys survive.
    raw = message.model_dump(mode="json")
    for part in raw.get("parts", []):
        if part.get("type") != "file":
            continue
        uri = part.get("uri", "")
        upload_id = parse_upload_uri(uri)
        if upload_id:
            # User upload: the Server owns the bytes, so there is never a
            # ``local_path`` hint. Small content is inlined (one less RPC
            # on the daemon); anything else is resolved on demand via
            # ``uploads.get``. A missing row is left unresolved on purpose
            # — the daemon fails the Step loudly rather than running with
            # a silently missing input.
            upload = await db.get(InputUploadModel, upload_id)
            if upload is None:
                continue
            part["name"] = part.get("name") or upload.name
            if not part.get("mime_type"):
                part["mime_type"] = upload.mime_type or None
            if upload.inline_bytes is not None:
                import base64

                part["inline_b64"] = base64.b64encode(upload.inline_bytes).decode()
            continue
        artifact_id = parse_artifact_uri(uri)
        if not artifact_id:
            continue
        artifact = await db.get(ArtifactModel, artifact_id)
        if artifact is None:
            continue
        if artifact.inline_bytes is not None:
            import base64

            part["inline_b64"] = base64.b64encode(artifact.inline_bytes).decode()
        elif artifact.origin_daemon_id == daemon_id and artifact.local_path:
            part["local_path"] = artifact.local_path
        if artifact.mime_type:
            part.setdefault("mime_type", artifact.mime_type)

    return raw, render_message(message)


def _canonical_context_json(value: dict) -> str:
    import json

    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _render_identity_md(
    db: AsyncSession, candidate: StepModel,
) -> str | None:
    """Render the identity AGENTS.md body for the claimed Step.

    Returns ``None`` when the Step has no identity, or when the
    referenced identity has been deleted since submit (claim is best-
    effort — never fails on a missing identity).
    """
    if candidate.identity_did is None:
        return None
    ident = await get_identity(candidate.identity_did, db)
    if ident is None:
        log.warning(
            "step %s references missing identity %s; "
            "claiming without identity injection",
            candidate.step_id, candidate.identity_did,
        )
        return None
    return _render_agent_md(ident)


__all__ = ["claim_steps_for_daemon"]
