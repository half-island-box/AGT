"""Task submission, claiming, reporting, status routes."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db, session_scope
from ..models import RuntimeModel, TaskModel
from ..schemas import (
    ClaimRequest,
    ClaimResponse,
    ProgressRequest,
    ProgressResponse,
    ReportRequest,
    ReportResponse,
    StreamDeltaEvent,
    StreamDoneEvent,
    StreamSnapshotEvent,
    SubmitTaskRequest,
    SubmitTaskResponse,
    TaskStatusResponse,
)
from ..services.claim_service import claim_tasks_for_daemon
from ..services.report_service import (
    record_task_progress_for_daemon,
    record_task_result_for_daemon,
)
from ..services.runtime_service import is_stale
from ..services.selection_service import SelectionError, select_runtime
from ..stream_hub import default_hub
from .identities import _selection_error_to_http

router = APIRouter(prefix="/api/tasks", tags=["tasks"])

# Terminal statuses: an SSE stream may close immediately once the task
# reaches one of these.
_TERMINAL_STATUSES = ("completed", "failed", "timeout", "cancelled")

# SSE keep-alive: emit a comment frame this often so proxies don't idle
# the connection out between deltas.
_SSE_PING_SECONDS = 15.0


def _business_error_to_http(exc: ValueError) -> HTTPException:
    """Map a service ``ValueError(dict)`` to the HTTP shape shared by the
    report and progress routes (``task_not_found`` → 404, ``task_not_owned``
    → 403, anything else → 400)."""
    body = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
    err_type = body.get("type")
    if err_type == "task_not_found":
        return HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="task not found"
        )
    if err_type == "task_not_owned":
        return HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="task not owned by this daemon",
        )
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(body))


def _new_task_id() -> str:
    return str(uuid.uuid4())[:8]


@router.post("/submit", response_model=SubmitTaskResponse, status_code=status.HTTP_201_CREATED)
async def submit_task(
    payload: SubmitTaskRequest,
    db: AsyncSession = Depends(get_db),
) -> SubmitTaskResponse:
    """Insert a task in pending state.

    If ``payload.task_id`` matches a terminal task (completed/failed/
    timeout), it is reopened as a fresh pending turn so the same task_id
    can drive session continuity across turns. An *active* (pending/
    running) task_id is rejected with 409.

    Phase 4 Step 4: when ``payload.identity_did`` is set, the server
    resolves the actual (provider, target_daemon_id) via
    ``select_runtime`` — ``payload.provider`` becomes the runtime_hint;
    the result overrides the user-provided values. Errors map to 404
    (not_found) / 400 (other) via ``_selection_error_to_http``.
    """
    effective_provider = payload.provider
    effective_target = payload.target_daemon_id

    # Phase 4 Step 4: identity resolves the runtime + validates binding.
    # MUST run before the target_daemon live-runtime check, because the
    # selection result determines which daemon we check against.
    if payload.identity_did:
        try:
            result = await select_runtime(
                payload.identity_did,
                payload.provider,
                "local",  # placeholder; select_runtime derives daemon from bindings
                db,
            )
        except SelectionError as exc:
            raise _selection_error_to_http(exc)
        effective_provider = result.provider
        effective_target = result.daemon_id

    # Step 3.6: when a target daemon is named, require that daemon to have
    # registered the requested provider — otherwise the task would sit
    # pending forever (the target can never claim it). Free tasks skip this.
    # Validated up front so it applies to both new and reopened tasks.
    # We also exclude rows whose ``last_seen`` is past the liveness TTL:
    # such rows belong to a daemon that's no longer reachable, so the
    # task would never be claimed.
    if effective_target:
        provider_rows = (
            await db.execute(
                select(RuntimeModel).where(
                    RuntimeModel.daemon_id == effective_target,
                    RuntimeModel.provider == effective_provider,
                )
            )
        ).scalars().all()
        has_live = any(not is_stale(r.last_seen) for r in provider_rows)
        if not has_live:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"daemon '{effective_target}' has no live runtime "
                    f"for provider '{effective_provider}'"
                ),
            )

    task_id = payload.task_id or _new_task_id()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    existing = await db.get(TaskModel, task_id)
    if existing is not None:
        if existing.status in ("pending", "running"):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"task_id '{task_id}' is already active",
            )
        # Terminal task → reopen for a resume turn. Reset execution fields
        # but keep the session pointer (SessionModel) so the next claim
        # resumes the same agent session.
        existing.provider = effective_provider
        existing.prompt = payload.prompt
        existing.status = "pending"
        existing.target_daemon_id = effective_target
        existing.daemon_id = None
        existing.exit_code = None
        existing.stdout = None
        existing.stderr = None
        existing.duration = None
        # Phase 4 workdir: refresh work_dir the same way as a fresh submit
        # (NULL for remote targets, persisted for local/free).
        existing.work_dir = _server_work_dir(payload)
        # cwd injection: forward the user-supplied agent_cwd verbatim —
        # it's a path the *daemon* will resolve, not the server.
        existing.agent_cwd = payload.agent_cwd
        existing.identity_did = payload.identity_did
        existing.updated_at = now
        await db.commit()
        return SubmitTaskResponse(task_id=task_id, status="pending")

    db.add(
        TaskModel(
            task_id=task_id,
            provider=effective_provider,
            prompt=payload.prompt,
            status="pending",
            target_daemon_id=effective_target,
            work_dir=_server_work_dir(payload),
            agent_cwd=payload.agent_cwd,
            identity_did=payload.identity_did,
            created_at=now,
            updated_at=now,
        )
    )
    await db.commit()
    return SubmitTaskResponse(task_id=task_id, status="pending")


def _server_work_dir(payload: SubmitTaskRequest) -> str | None:
    """Decide whether the server should persist ``work_dir``.

    Persist only when the task targets this server's local daemon (free
    task, ``target_daemon_id is None``) OR targets an explicit local daemon
    whose id matches the running server (caller-supplied). For remote
    targets, NULL — the receiving daemon supplies its own fallback path.
    """
    if payload.target_daemon_id is None:
        return payload.work_dir
    # When ``target_daemon_id`` is set we can't reliably know whether it
    # refers to a local or remote daemon without server self-knowledge.
    # Persistence is opt-in via the payload: a remote-targeted task
    # explicitly receives ``work_dir=None`` from the CLI submit path, so
    # anything non-None here came from a caller that knows the target is
    # local. We trust it as-is.
    return payload.work_dir


@router.post("/claim", response_model=ClaimResponse)
async def claim_task(
    payload: ClaimRequest,
    db: AsyncSession = Depends(get_db),
) -> ClaimResponse:
    """HTTP entry point for ``claim_tasks_for_daemon``.

    The wire format (``{daemon_id, max_concurrent, timeout_seconds,
    runtime_ids}`` → ``{tasks: [...]}``) is preserved for backward
    compatibility. The reverse-dial ``DaemonLink`` (server side) uses
    the same service function over WebSocket.
    """
    tasks = await claim_tasks_for_daemon(
        db,
        daemon_id=payload.daemon_id,
        max_tasks=payload.max_concurrent,
        timeout_seconds=payload.timeout_seconds,
        runtime_ids=payload.runtime_ids,
    )
    return ClaimResponse(tasks=tasks)


@router.post("/{task_id}/report")
async def report_task(
    task_id: str,
    payload: ReportRequest,
    db: AsyncSession = Depends(get_db),
) -> ReportResponse:
    """Daemon reports the result of an executed task.

    Business logic lives in :func:`record_task_result_for_daemon` so
    the HTTP path and the reverse-dial WS path stay in lock-step.
    """
    try:
        result = await record_task_result_for_daemon(
            db,
            daemon_id=payload.daemon_id,
            task_id=task_id,
            exit_code=payload.exit_code,
            stdout=payload.stdout,
            stderr=payload.stderr,
            duration=payload.duration,
            status=payload.status,
            session_id=payload.session_id,
            resume_rejected=payload.resume_rejected,
        )
        return ReportResponse(**result)
    except ValueError as exc:
        raise _business_error_to_http(exc)


@router.post("/{task_id}/progress", response_model=ProgressResponse)
async def progress_task(
    task_id: str,
    payload: ProgressRequest,
    db: AsyncSession = Depends(get_db),
) -> ProgressResponse:
    """Daemon reports an incremental stdout prefix mid-execution.

    HTTP fallback entry for ``record_task_progress_for_daemon`` — the
    reverse-dial WS path routes to the same service function.
    """
    try:
        result = await record_task_progress_for_daemon(
            db,
            daemon_id=payload.daemon_id,
            task_id=task_id,
            stdout_prefix=payload.stdout_prefix,
        )
        return ProgressResponse(**result)
    except ValueError as exc:
        raise _business_error_to_http(exc)


@router.get("")
async def list_tasks(
    statuses: str | None = Query(
        default=None,
        description="Comma-separated status filter (e.g. 'completed,failed,timeout'). "
                    "Omit for all.",
    ),
    limit: int = Query(default=200, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[TaskStatusResponse]:
    """List tasks (Phase 4: backs ``agt task clean --all-done``)."""
    stmt = select(TaskModel).order_by(TaskModel.updated_at.desc()).limit(limit)
    if statuses:
        wanted = [s.strip() for s in statuses.split(",") if s.strip()]
        if wanted:
            stmt = stmt.where(TaskModel.status.in_(wanted))
    rows = (await db.execute(stmt)).scalars().all()
    return [
        TaskStatusResponse(
            task_id=t.task_id,
            provider=t.provider,
            status=t.status,
            session_id=t.session_id,
            resume_rejected=t.resume_rejected,
            exit_code=t.exit_code,
            stdout=t.stdout,
            stderr=t.stderr,
            duration=t.duration,
            created_at=t.created_at,
            updated_at=t.updated_at,
            daemon_id=t.daemon_id,
            target_daemon_id=t.target_daemon_id,
            work_dir=t.work_dir,
            agent_cwd=t.agent_cwd,
        )
        for t in rows
    ]


@router.get("/{task_id}/status")
async def task_status(
    task_id: str,
    db: AsyncSession = Depends(get_db),
) -> TaskStatusResponse:
    task = await db.get(TaskModel, task_id)
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="task not found")
    return TaskStatusResponse(
        task_id=task.task_id,
        provider=task.provider,
        status=task.status,
        session_id=task.session_id,
        resume_rejected=task.resume_rejected,
        exit_code=task.exit_code,
        stdout=task.stdout,
        stderr=task.stderr,
        identity_did=task.identity_did,
        duration=task.duration,
        created_at=task.created_at,
        updated_at=task.updated_at,
        daemon_id=task.daemon_id,
        target_daemon_id=task.target_daemon_id,
        work_dir=task.work_dir,
        agent_cwd=task.agent_cwd,
    )


def _sse_frame(event_type: str, payload) -> str:
    """Serialize one SSE frame (``event:`` line + JSON ``data:`` line)."""
    data = (
        payload.model_dump_json()
        if hasattr(payload, "model_dump_json")
        else json.dumps(payload)
    )
    return f"event: {event_type}\ndata: {data}\n\n"


@router.get("/{task_id}/stream")
async def stream_task(
    task_id: str,
    request: Request,
) -> StreamingResponse:
    """SSE stream of a task's execution (Phase 5 remote streaming).

    Event sequence: ``snapshot`` → ``delta``* → ``done``. The snapshot and
    the 404 check are performed **here, in the endpoint body**, using a
    short-lived session that is closed before the ``StreamingResponse``
    generator starts running (design decision 9). The generator therefore
    never touches the DB; it only drains the in-process ``StreamHub`` queue.
    """
    factory = request.app.state.session_factory
    async with session_scope(factory) as db:
        task = await db.get(TaskModel, task_id)
        if task is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="task not found"
            )

        snapshot_stdout = task.stdout or ""
        snapshot_status = task.status
        is_terminal = task.status in _TERMINAL_STATUSES

        # Captured now (session open) for the terminal shortcut below.
        done_payload = {
            "status": task.status,
            "stdout": task.stdout or "",
            "stderr": task.stderr or "",
            "session_id": task.session_id,
            "exit_code": task.exit_code,
            "resume_rejected": task.resume_rejected,
        }

    async def event_generator():
        sub = None
        try:
            yield _sse_frame(
                "snapshot",
                StreamSnapshotEvent(
                    status=snapshot_status, stdout=snapshot_stdout
                ),
            )
            if is_terminal:
                yield _sse_frame("done", StreamDoneEvent(**done_payload))
                return

            sub = default_hub.subscribe(
                task_id, cursor=len(snapshot_stdout)
            )
            while True:
                try:
                    event = await asyncio.wait_for(
                        sub.queue.get(), timeout=_SSE_PING_SECONDS
                    )
                except asyncio.TimeoutError:
                    # Keep-alive comment frame; also lets a hung reader
                    # on the client side detect a dead connection.
                    yield ": ping\n\n"
                    continue
                etype = event.get("type")
                if etype == "delta":
                    yield _sse_frame(
                        "delta", StreamDeltaEvent(chunk=event["chunk"])
                    )
                elif etype == "done":
                    yield _sse_frame("done", StreamDoneEvent(**event))
                    return
        finally:
            if sub is not None:
                default_hub.unsubscribe(task_id, sub)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


__all__ = ["router"]
