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
    cwds: list[str] | None = None,
    daemon_id: str | None = None,
    provider: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[ChatRunSummary], int]:
    """List chat Jobs as run rows, newest update first.

    The Job — not the provider session pointer — is the chat identity. A
    provider ``session_id`` only exists after a runtime reports one, and a
    first turn may be cancelled, time out, or still be queued/running before
    that happens. Listing Jobs (LEFT JOIN semantics) keeps those
    conversations visible instead of dropping them until some later turn
    succeeds.
    """
    stmt = select(JobModel).where(JobModel.job_type == "chat")
    if cwds:
        stmt = stmt.where(
            JobModel.cwd.in_(cwds)
            | exists(
                select(SessionModel.job_id).where(
                    SessionModel.job_id == JobModel.job_id,
                    SessionModel.cwd.in_(cwds),
                )
            )
        )
    if daemon_id is not None:
        stmt = stmt.where(
            (JobModel.target_daemon_id == daemon_id)
            | exists(
                select(StepModel.step_id).where(
                    StepModel.job_id == JobModel.job_id,
                    StepModel.daemon_id == daemon_id,
                )
            )
        )
    if provider is not None:
        stmt = stmt.where(
            (JobModel.provider == provider)
            | exists(
                select(SessionModel.job_id).where(
                    SessionModel.job_id == JobModel.job_id,
                    SessionModel.provider == provider,
                )
            )
        )

    stmt = stmt.order_by(JobModel.updated_at.desc(), JobModel.job_id.desc())
    rows = (await db.execute(stmt)).scalars().all()

    total = len(rows)
    page_jobs = rows[offset : offset + limit]
    job_ids = [job.job_id for job in page_jobs]
    sessions_by_job: dict[str, SessionModel] = {}
    if job_ids:
        session_rows = (
            (
                await db.execute(
                    select(SessionModel)
                    .where(SessionModel.job_id.in_(job_ids))
                    .order_by(SessionModel.updated_at.desc())
                )
            )
            .scalars()
            .all()
        )
        for session in session_rows:
            sessions_by_job.setdefault(session.job_id, session)

    runs: list[ChatRunSummary] = []
    for job in page_jobs:
        session = sessions_by_job.get(job.job_id)
        runs.append(
            ChatRunSummary(
                root_job_id=job.job_id,
                session_id=session.session_id if session is not None else None,
                provider=session.provider if session is not None else job.provider,
                identity_did=(
                    session.identity_did or job.identity_did
                    if session is not None
                    else job.identity_did
                ),
                daemon_id=job.target_daemon_id,
                cwd=(session.cwd or job.cwd) if session is not None else job.cwd,
                title=_first_prompt(job),
                status=job.status,
                created_at=(
                    session.created_at if session is not None else job.created_at
                ),
                updated_at=(
                    session.updated_at if session is not None else job.updated_at
                ),
            )
        )
    return runs, total


async def list_chat_runs_response(
    db: AsyncSession,
    *,
    cwds: list[str] | None = None,
    daemon_id: str | None = None,
    provider: str | None = None,
    limit: int,
    offset: int,
) -> ChatRunListResponse:
    runs, total = await list_chat_runs(
        db,
        cwds=cwds,
        daemon_id=daemon_id,
        provider=provider,
        limit=limit,
        offset=offset,
    )
    return ChatRunListResponse(runs=runs, total=total)
