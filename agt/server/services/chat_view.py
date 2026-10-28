"""Read-side helpers for Chat-mode run list APIs."""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import JobModel, SessionModel, StepModel
from ..schemas import ChatRunListResponse, ChatRunSummary


def _first_prompt(job: JobModel) -> str | None:
    """Return the conversation title, falling back to its earliest Step.

    ``first_prompt`` is persisted when a chat Job is created. The Step
    fallback keeps runs created by old servers visible before the backfill
    migration runs (or when a test/legacy fixture constructs rows directly).
    """
    prompt = job.first_prompt
    if prompt is None:
        prompt = next((step.prompt for step in job.steps if step.prompt), None)
    return prompt if prompt and prompt.strip() else None


async def list_chat_runs(
    db: AsyncSession,
    *,
    cwd: str | None = None,
    daemon_id: str | None = None,
    provider: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[ChatRunSummary], int]:
    """List chat sessions as run rows, newest update first.

    Sessions are the canonical chat index; we join JobModel for status and
    daemon context. The ``cwd`` filter uses ``SessionModel.cwd`` when
    available, falling back to the associated JobModel.cwd.
    """
    stmt = (
        select(SessionModel, JobModel)
        .join(JobModel, SessionModel.job_id == JobModel.job_id)
        .where(JobModel.job_type == "chat")
    )
    if cwd is not None:
        stmt = stmt.where(
            (SessionModel.cwd == cwd) | (SessionModel.cwd.is_(None) & (JobModel.cwd == cwd))
        )
    if daemon_id is not None:
        stmt = stmt.where(
            (JobModel.target_daemon_id == daemon_id)
            | exists(
                select(StepModel.step_id).where(
                    StepModel.job_id == SessionModel.job_id,
                    StepModel.daemon_id == daemon_id,
                )
            )
        )
    if provider is not None:
        stmt = stmt.where(SessionModel.provider == provider)

    stmt = stmt.order_by(SessionModel.updated_at.desc(), SessionModel.job_id.desc())
    rows = (await db.execute(stmt)).all()

    total = len(rows)
    page_rows = rows[offset : offset + limit]

    runs: list[ChatRunSummary] = []
    for session, job in page_rows:
        runs.append(
            ChatRunSummary(
                root_job_id=job.job_id,
                session_id=session.session_id,
                provider=session.provider,
                identity_did=session.identity_did or job.identity_did,
                daemon_id=job.target_daemon_id,
                cwd=session.cwd or job.cwd,
                title=_first_prompt(job),
                status=job.status,
                created_at=session.created_at,
                updated_at=session.updated_at,
            )
        )
    return runs, total


async def list_chat_runs_response(
    db: AsyncSession,
    *,
    cwd: str | None = None,
    daemon_id: str | None = None,
    provider: str | None = None,
    limit: int,
    offset: int,
) -> ChatRunListResponse:
    runs, total = await list_chat_runs(
        db,
        cwd=cwd,
        daemon_id=daemon_id,
        provider=provider,
        limit=limit,
        offset=offset,
    )
    return ChatRunListResponse(runs=runs, total=total)
