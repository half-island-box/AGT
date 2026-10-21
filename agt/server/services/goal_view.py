"""Read-side helpers for Goal-mode list and tree APIs.

Submission and orchestration live in ``goal_service`` / ``job_service``.
These helpers stay read-only and shared by both Goal and Job routes so the
plugin gets the same Goal metadata shape everywhere.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...task.spawn_intent import parse_ask_markers
from ..models import JobModel
from ..schemas import GoalBudgetSummary, GoalMetaSummary, GoalRunSummary


def parse_goal_meta(job: JobModel) -> dict | None:
    """Return a valid goal-root metadata object, or None."""
    if not job.goal_meta:
        return None
    try:
        meta = json.loads(job.goal_meta)
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("mode") == "goal" else None


def goal_meta_summary(job: JobModel) -> GoalMetaSummary | None:
    """Build the safe subset of ``goal_meta`` exposed to clients."""
    meta = parse_goal_meta(job)
    if meta is None:
        return None

    raw_budget = meta.get("budget")
    budget: GoalBudgetSummary | None = None
    if isinstance(raw_budget, dict):
        try:
            budget = GoalBudgetSummary(
                max_depth=int(raw_budget.get("max_depth", 0)),
                max_descendants=int(raw_budget.get("max_descendants", 0)),
                max_batch=int(raw_budget.get("max_batch", 0)),
            )
        except (TypeError, ValueError):
            budget = None

    goal = meta.get("goal")
    template = meta.get("template")
    sha = meta.get("goal_sha256")
    return GoalMetaSummary(
        goal=goal if isinstance(goal, str) else "",
        template=template if isinstance(template, str) else None,
        budget=budget,
        goal_sha256=sha if isinstance(sha, str) else None,
    )


def build_goal_run_summary(
    root: JobModel, nodes: list[JobModel]
) -> GoalRunSummary | None:
    """Build a history-list row from a root and every node under it."""
    meta = goal_meta_summary(root)
    if meta is None:
        return None

    status_counts: dict[str, int] = {}
    for node in nodes:
        status_counts[node.status] = status_counts.get(node.status, 0) + 1

    return GoalRunSummary(
        root_job_id=root.job_id,
        goal=meta.goal,
        status=root.status,
        provider=root.provider,
        identity_did=root.identity_did,
        daemon_id=root.target_daemon_id,
        cwd=root.cwd,
        created_at=root.created_at,
        updated_at=root.updated_at,
        total_nodes=len(nodes),
        status_counts=status_counts,
        meta=meta,
    )


async def list_goal_runs(
    db: AsyncSession,
    *,
    cwd: str | None = None,
    daemon_id: str | None = None,
    limit: int,
    offset: int,
) -> tuple[list[GoalRunSummary], int]:
    """List persisted goal roots, newest update first, with tree summaries."""
    filters = [
        JobModel.root_job_id == JobModel.job_id,
        JobModel.job_type == "goal",
    ]
    if cwd is not None:
        filters.append(JobModel.cwd == cwd)
    if daemon_id is not None:
        filters.append(JobModel.target_daemon_id == daemon_id)
    roots = (
        (
            await db.execute(
                select(JobModel)
                .where(*filters)
                .order_by(JobModel.updated_at.desc(), JobModel.job_id.desc())
            )
        )
        .scalars()
        .all()
    )
    valid_roots = [root for root in roots if parse_goal_meta(root) is not None]
    total = len(valid_roots)
    roots = valid_roots[offset : offset + limit]

    root_ids = [root.job_id for root in roots]
    nodes = (
        (
            await db.execute(
                select(JobModel)
                .where(JobModel.root_job_id.in_(root_ids))
                .order_by(JobModel.created_at, JobModel.job_id)
            )
        )
        .scalars()
        .all()
    )
    nodes_by_root: dict[str, list[JobModel]] = {}
    for node in nodes:
        nodes_by_root.setdefault(node.root_job_id, []).append(node)

    runs = [
        summary
        for root in roots
        if (summary := build_goal_run_summary(root, nodes_by_root.get(root.job_id, [])))
        is not None
    ]
    return runs, total


def current_task_id(job: JobModel) -> str | None:
    """Return the active Step id, or the latest Step when already terminal.

    The plugin uses this to subscribe to a node's SSE stream only while the
    Job is actually running; terminal nodes can use the normal job log.
    """
    for step in reversed(job.steps):
        if step.status in ("pending", "running"):
            return step.task_id
    return job.steps[-1].task_id if job.steps else None


def ask_question(job: JobModel) -> str | None:
    """Return the pending AGT-ASK question for a ``waiting_input`` Job.

    Reports normally carry the question in ``outcome_payload.ask``. The
    stdout fallback keeps the UI working with reports from older daemons.
    """
    if job.status != "waiting_input":
        return None
    for step in reversed(job.steps):
        if not step.outcome_payload:
            continue
        try:
            payload = json.loads(step.outcome_payload)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            ask = payload.get("ask")
            if isinstance(ask, dict):
                question = ask.get("question")
                if isinstance(question, str) and question.strip():
                    return question.strip()
    for step in reversed(job.steps):
        question = parse_ask_markers(step.stdout or "").question
        if question:
            return question
    return None


__all__ = [
    "ask_question",
    "build_goal_run_summary",
    "current_task_id",
    "goal_meta_summary",
    "list_goal_runs",
    "parse_goal_meta",
]
