"""Goal child spawning shared by MCP tool calls and the marker fallback.

The service owns the trust boundary and all durable spawn semantics:

- resolve the calling Step from a task-scoped bearer token;
- validate envelopes and enforce the root Job budget;
- create child Jobs immediately;
- persist an idempotent spawn batch so tool retries cannot duplicate work.

MCP transport lives in :mod:`agt.server.mcp_server` and
:mod:`agt.daemon.mcp_proxy`; the legacy ``AGT-SPAWN`` parser remains a
compatibility path that calls the same service.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
import weakref
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import DataPart, FilePart, Message, TextPart, render_message
from ..models import (
    IdentityModel,
    JobModel,
    SpawnBatchModel,
    StepModel,
)
from .auth_service import verify_task_token_claims
from .job_service import (
    MAX_ROOT_CHILDREN,
    MAX_SPAWN_BATCH,
    MAX_TREE_DEPTH,
)
from .selection_service import route_runtime

log = logging.getLogger(__name__)

SPAWN_TOOL_DESCRIPTION = (
    "Create parallel child tasks for the current Goal. Use this tool instead "
    "of printing AGT-SPAWN when it is available. The server resolves the "
    "calling Goal from a task-scoped token; never invent a parent job id. "
    "A successful response is authoritative. Child execution continues "
    "asynchronously and the server triggers synthesis after all children "
    "finish. Inspect rejected entries before claiming the whole batch was "
    "accepted."
)


class SpawnChildInput(BaseModel):
    """One child envelope accepted by the MCP tool."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    goal: str = Field(min_length=1)
    identity: str | None = None
    inputs: list[str] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    return_: str | None = Field(default=None, alias="return")
    daemon_id: str | None = None
    provider: str | None = None


@dataclass(frozen=True)
class SpawnContext:
    """Trusted caller context derived from a task token."""

    step: StepModel
    job: JobModel
    daemon_id: str


@dataclass
class SpawnExecution:
    """A spawn response plus the ORM objects for same-transaction callers."""

    response: dict[str, Any]
    created_jobs: list[JobModel]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _new_spawn_id() -> str:
    return f"spawn-{uuid.uuid4().hex[:12]}"


def _canonical_children(children: list[dict]) -> str:
    return json.dumps(
        children,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _request_digest(children: list[dict]) -> str:
    return hashlib.sha256(_canonical_children(children).encode("utf-8")).hexdigest()


def _default_idempotency_key(parent_step_id: str | None, digest: str) -> str:
    if parent_step_id is None:
        return digest[:32]
    raw = f"{parent_step_id}\n{digest}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:32]


async def resolve_spawn_context(
    db: AsyncSession,
    *,
    task_token: str | None,
    secret: str,
    trusted_daemon_id: str | None = None,
    require_running: bool = True,
) -> SpawnContext:
    """Resolve and authorize the Goal Step that is calling spawn.

    ``trusted_daemon_id`` is supplied by the reverse-dial link. For local
    HTTP MCP calls it is ``None``, so the signed token itself provides
    the daemon identity.
    """
    if not secret:
        raise ValueError({
            "type": "spawn_unavailable",
            "code": "spawn_unavailable",
            "message": "server secret is not configured",
        })
    if not task_token:
        raise ValueError({
            "type": "unauthenticated",
            "code": "unauthenticated",
            "message": "task token required",
        })
    try:
        claims = verify_task_token_claims(task_token, secret)
    except PermissionError:
        raise ValueError({
            "type": "unauthenticated",
            "code": "unauthenticated",
            "message": "invalid task token",
        }) from None

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.startswith("agent:"):
        raise ValueError({
            "type": "unauthenticated",
            "code": "unauthenticated",
            "message": "token is not agent scoped",
        })
    daemon_id = subject.removeprefix("agent:")

    if trusted_daemon_id is not None and daemon_id != trusted_daemon_id:
        raise ValueError({
            "type": "task_not_owned",
            "code": "task_not_owned",
            "message": "task token belongs to another daemon",
        })

    step_id = claims["task_id"]
    step = await db.get(StepModel, step_id)
    if step is None or step.job_id is None:
        raise ValueError({
            "type": "task_not_found",
            "code": "task_not_found",
            "message": step_id,
        })
    if step.daemon_id != daemon_id:
        raise ValueError({
            "type": "task_not_owned",
            "code": "task_not_owned",
            "message": "task token does not own this Step",
        })
    if require_running and step.status != "running":
        raise ValueError({
            "type": "parent_not_running",
            "code": "parent_not_running",
            "message": f"step {step_id} is {step.status}",
        })

    job = await db.get(JobModel, step.job_id)
    if job is None:
        raise ValueError({
            "type": "job_not_found",
            "code": "job_not_found",
            "message": step.job_id,
        })
    if job.job_type != "goal":
        raise ValueError({
            "type": "not_goal_job",
            "code": "not_goal_job",
            "message": f"job {job.job_id} is {job.job_type}",
        })
    if require_running and job.status != "running":
        raise ValueError({
            "type": "parent_not_running",
            "code": "parent_not_running",
            "message": f"job {job.job_id} is {job.status}",
        })
    return SpawnContext(step=step, job=job, daemon_id=daemon_id)


_spawn_locks: weakref.WeakValueDictionary[
    tuple[int, str], asyncio.Lock
] = weakref.WeakValueDictionary()


def _spawn_lock(root_job_id: str) -> asyncio.Lock:
    """Return an event-loop-local root lock for budget/idempotency checks."""
    loop = asyncio.get_running_loop()
    key = (id(loop), root_job_id)
    lock = _spawn_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _spawn_locks[key] = lock
    return lock


async def _existing_batch(
    db: AsyncSession,
    *,
    parent_step_id: str,
    idempotency_key: str,
    request_digest: str,
) -> SpawnBatchModel | None:
    batch = (
        await db.execute(
            select(SpawnBatchModel).where(
                SpawnBatchModel.parent_step_id == parent_step_id,
                SpawnBatchModel.idempotency_key == idempotency_key,
            )
        )
    ).scalars().first()
    if batch is not None and batch.request_digest != request_digest:
        raise ValueError({
            "type": "idempotency_conflict",
            "code": "idempotency_conflict",
            "message": (
                "idempotency_key was already used for a different spawn request"
            ),
        })
    if batch is None:
        # A retry may omit the key or generate a new one. Identical child
        # envelopes from the same Step are still the same spawn batch.
        batch = (
            await db.execute(
                select(SpawnBatchModel).where(
                    SpawnBatchModel.parent_step_id == parent_step_id,
                    SpawnBatchModel.request_digest == request_digest,
                )
            )
        ).scalars().first()
    return batch


def _batch_response(batch: SpawnBatchModel) -> dict[str, Any]:
    try:
        result = json.loads(batch.result_json)
    except (TypeError, ValueError):
        result = {}
    if not isinstance(result, dict):
        result = {}
    result = dict(result)
    result["ok"] = True
    result["spawn_id"] = batch.spawn_id
    result["duplicate"] = True
    return result


async def _created_jobs_from_response(
    db: AsyncSession, response: dict[str, Any]
) -> list[JobModel]:
    ids = [
        item.get("child_job_id")
        for item in response.get("children", [])
        if isinstance(item, dict) and isinstance(item.get("child_job_id"), str)
    ]
    if not ids:
        return []
    jobs = (
        await db.execute(select(JobModel).where(JobModel.job_id.in_(ids)))
    ).scalars().all()
    by_id = {job.job_id: job for job in jobs}
    return [by_id[job_id] for job_id in ids if job_id in by_id]


def _normalize_envelope(envelope: Any) -> dict[str, Any] | None:
    if not isinstance(envelope, dict):
        return None
    goal = envelope.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        return None

    identity = envelope.get("identity")
    if identity is not None and not isinstance(identity, str):
        return None
    daemon_id = envelope.get("daemon_id")
    if daemon_id is not None and not isinstance(daemon_id, str):
        return None
    provider = envelope.get("provider")
    if provider is not None and not isinstance(provider, str):
        return None
    return_value = envelope.get("return")
    if return_value is not None and not isinstance(return_value, str):
        return None

    inputs = envelope.get("inputs")
    if inputs is None:
        inputs = []
    if not isinstance(inputs, list) or any(
        not isinstance(item, str) for item in inputs
    ):
        return None

    context_refs = envelope.get("context_refs", [])
    if not isinstance(context_refs, list) or any(
        not isinstance(item, str) for item in context_refs
    ):
        return None
    context_refs = [item for item in context_refs if item][:16]

    normalized: dict[str, Any] = {
        "goal": goal.strip(),
        "inputs": [item for item in inputs if item],
        "context_refs": context_refs,
    }
    if isinstance(identity, str) and identity:
        normalized["identity"] = identity
    if isinstance(daemon_id, str) and daemon_id:
        normalized["daemon_id"] = daemon_id
    if isinstance(provider, str) and provider:
        normalized["provider"] = provider
    if isinstance(return_value, str) and return_value:
        normalized["return"] = return_value
    return normalized


async def _job_depth(
    db: AsyncSession, job: JobModel, max_depth: int = MAX_TREE_DEPTH
) -> int:
    depth = 1
    current = job
    while current.parent_job_id is not None and depth <= max_depth + 1:
        parent = await db.get(JobModel, current.parent_job_id)
        if parent is None:
            break
        current = parent
        depth += 1
    return depth


async def _goal_budget(
    db: AsyncSession, root_job_id: str
) -> tuple[int, int, int]:
    max_depth, max_descendants, max_batch = (
        MAX_TREE_DEPTH, MAX_ROOT_CHILDREN, MAX_SPAWN_BATCH,
    )
    root = await db.get(JobModel, root_job_id)
    if root is None or not root.goal_meta:
        return max_depth, max_descendants, max_batch
    try:
        meta = json.loads(root.goal_meta)
    except (TypeError, ValueError):
        return max_depth, max_descendants, max_batch
    budget = meta.get("budget") if isinstance(meta, dict) else None
    if not isinstance(budget, dict):
        return max_depth, max_descendants, max_batch

    def _int(key: str, default: int) -> int:
        value = budget.get(key)
        return value if isinstance(value, int) and value >= 1 else default

    return (
        _int("max_depth", MAX_TREE_DEPTH),
        _int("max_descendants", MAX_ROOT_CHILDREN),
        _int("max_batch", MAX_SPAWN_BATCH),
    )


def _rejected(index: int, code: str, message: str) -> dict[str, Any]:
    return {"index": index, "code": code, "message": message}


async def spawn_children_detailed(
    db: AsyncSession,
    *,
    parent_job: JobModel,
    children: list[dict],
    daemon_id: str,
    parent_step_id: str | None = None,
    idempotency_key: str | None = None,
    strict: bool = True,
) -> SpawnExecution:
    """Validate, budget, and materialize child Jobs.

    ``strict=True`` is the MCP path: over-limit children are returned in
    ``rejected`` instead of being silently dropped. ``strict=False`` keeps
    the legacy marker behavior (truncate to the effective budget).
    """
    if not children:
        raise ValueError({
            "type": "invalid_spawn_request",
            "code": "invalid_spawn_request",
            "message": "children must contain at least one child",
        })
    async with _spawn_lock(parent_job.root_job_id):
        digest = _request_digest(children)
        effective_key = idempotency_key or _default_idempotency_key(
            parent_step_id, digest
        )
        if parent_step_id is not None:
            existing = await _existing_batch(
                db,
                parent_step_id=parent_step_id,
                idempotency_key=effective_key,
                request_digest=digest,
            )
            if existing is not None:
                response = _batch_response(existing)
                return SpawnExecution(
                    response=response,
                    created_jobs=await _created_jobs_from_response(db, response),
                )
        if parent_job.status != "running":
            raise ValueError({
                "type": "parent_not_running",
                "code": "parent_not_running",
                "message": f"job {parent_job.job_id} is {parent_job.status}",
            })

        max_depth, max_descendants, max_batch = await _goal_budget(
            db, parent_job.root_job_id
        )

        rejected: list[dict[str, Any]] = []
        normalized: list[tuple[int, dict[str, Any]]] = []
        for index, envelope in enumerate(children):
            item = _normalize_envelope(envelope)
            if item is None:
                rejected.append(_rejected(
                    index,
                    "invalid_spawn_request",
                    "child must be an object with a non-empty goal",
                ))
                continue
            if len(normalized) >= max_batch:
                rejected.append(_rejected(
                    index,
                    "batch_too_large",
                    f"max_batch is {max_batch}",
                ))
                continue
            normalized.append((index, item))

        depth = await _job_depth(db, parent_job, max_depth)
        if normalized and depth + 1 > max_depth:
            for index, _ in normalized:
                rejected.append(_rejected(
                    index,
                    "depth_exhausted",
                    f"max_depth is {max_depth}",
                ))
            normalized = []

        existing_descendants = (
            await db.execute(
                select(JobModel).where(
                    JobModel.root_job_id == parent_job.root_job_id,
                    JobModel.parent_job_id.is_not(None),
                )
            )
        ).scalars().all()
        remaining = max_descendants - len(existing_descendants)
        if remaining <= 0 and normalized:
            for index, _ in normalized:
                rejected.append(_rejected(
                    index,
                    "descendant_budget_exhausted",
                    f"max_descendants is {max_descendants}",
                ))
            normalized = []
        elif len(normalized) > remaining:
            allowed = normalized[:remaining]
            for index, _ in normalized[remaining:]:
                rejected.append(_rejected(
                    index,
                    "descendant_budget_exhausted",
                    f"max_descendants is {max_descendants}",
                ))
            normalized = allowed

        if not strict and rejected:
            # Marker fallback preserves the old best-effort behavior: log
            # over-limit envelopes but do not surface them to a user.
            log.warning(
                "spawn rejected %d envelope(s) (parent=%s): %s",
                len(rejected),
                parent_job.job_id,
                ", ".join(item["code"] for item in rejected),
            )

        created: list[JobModel] = []
        child_results: list[dict[str, Any]] = []
        for index, envelope in normalized:
            child, birth_error = await _create_child(
                db,
                parent_job=parent_job,
                envelope=envelope,
                daemon_id=daemon_id,
            )
            created.append(child)
            result_item: dict[str, Any] = {
                "index": index,
                "child_job_id": child.job_id,
                "status": child.status,
                "daemon_id": child.target_daemon_id,
                "provider": child.provider,
            }
            if birth_error:
                result_item["error"] = birth_error
            child_results.append(result_item)

        spawn_id = _new_spawn_id()
        response: dict[str, Any] = {
            "ok": True,
            "spawn_id": spawn_id,
            "duplicate": False,
            "accepted": len(created),
            "children": child_results,
            "rejected": rejected,
        }

        if parent_step_id is not None:
            now = _now()
            db.add(SpawnBatchModel(
                spawn_id=spawn_id,
                parent_step_id=parent_step_id,
                parent_job_id=parent_job.job_id,
                root_job_id=parent_job.root_job_id,
                idempotency_key=effective_key,
                request_digest=digest,
                result_json=json.dumps(response, ensure_ascii=False),
                created_at=now,
                updated_at=now,
            ))
            await db.flush()

        return SpawnExecution(response=response, created_jobs=created)


async def _create_child(
    db: AsyncSession,
    *,
    parent_job: JobModel,
    envelope: dict[str, Any],
    daemon_id: str,
) -> tuple[JobModel, str | None]:
    """Create one child, including deterministic failed-at-birth handling."""
    identity = envelope.get("identity") or None
    provider = parent_job.provider
    unknown_identity: str | None = None
    if identity:
        identity_row = await db.get(IdentityModel, identity)
        if identity_row is None or identity_row.status != "active":
            unknown_identity = identity
            identity = None

    req_daemon = envelope.get("daemon_id")
    req_provider = envelope.get("provider")
    routed = await route_runtime(
        db,
        identity_did=identity,
        provider_hint=parent_job.provider,
        requested_daemon_id=req_daemon,
        requested_provider=req_provider,
    )
    placement_failure: str | None = None
    if routed is not None:
        target_daemon = routed.daemon_id
        provider = routed.provider
    elif req_daemon is not None or req_provider is not None:
        placement_failure = (
            f"requested placement unavailable: daemon_id={req_daemon}, "
            f"provider={req_provider}"
        )
        target_daemon = daemon_id
        provider = parent_job.provider
    else:
        target_daemon = daemon_id

    same_daemon = target_daemon == daemon_id
    child_agent_cwd = parent_job.agent_cwd if same_daemon else None
    child_work_dir = parent_job.work_dir if same_daemon else None

    # Import lazily to keep the job-service compatibility wrapper acyclic.
    from .job_service import create_job

    message = _child_message(envelope)

    child = await create_job(
        db,
        provider=provider,
        prompt=render_message(message),
        target_daemon_id=target_daemon,
        work_dir=child_work_dir,
        agent_cwd=child_agent_cwd,
        identity_did=identity,
        job_type=parent_job.job_type,
        message=message,
        parent_job_id=parent_job.job_id,
    )

    reason = placement_failure or (
        f"unknown identity: {unknown_identity}" if unknown_identity else None
    )
    if reason is not None:
        child.status = "failed"
        child.updated_at = _now()
        step = child.steps[0]
        step.status = "failed"
        step.outcome = "error"
        step.stderr = reason
        step.updated_at = _now()
        await db.flush()
    return child, reason


def _child_message(envelope: dict) -> Message:
    parts: list = [TextPart(text=str(envelope.get("goal") or ""))]
    data: dict[str, Any] = {}
    if envelope.get("identity"):
        data["identity"] = envelope["identity"]
    if envelope.get("inputs"):
        data["inputs"] = list(envelope["inputs"])
    if envelope.get("context_refs"):
        data["context_refs"] = list(envelope["context_refs"])
    if envelope.get("return"):
        data["return"] = envelope["return"]
    if data:
        parts.append(DataPart(data=data))
    inputs = envelope.get("inputs") or []
    if isinstance(inputs, list):
        for uri in inputs:
            if isinstance(uri, str) and uri:
                parts.append(FilePart(uri=uri))
    return Message(role="user", parts=parts)


async def spawn_children_for_token(
    db: AsyncSession,
    *,
    task_token: str | None,
    secret: str,
    children: list[dict],
    idempotency_key: str | None = None,
    trusted_daemon_id: str | None = None,
) -> dict[str, Any]:
    """Authenticated entry point used by the MCP transport."""
    context = await resolve_spawn_context(
        db,
        task_token=task_token,
        secret=secret,
        trusted_daemon_id=trusted_daemon_id,
        require_running=False,
    )
    digest = _request_digest(children)
    effective_key = idempotency_key or _default_idempotency_key(
        context.step.step_id, digest
    )
    existing = await _existing_batch(
        db,
        parent_step_id=context.step.step_id,
        idempotency_key=effective_key,
        request_digest=digest,
    )
    if existing is not None:
        response = _batch_response(existing)
        return response
    if context.step.status != "running":
        raise ValueError({
            "type": "parent_not_running",
            "code": "parent_not_running",
            "message": f"step {context.step.step_id} is {context.step.status}",
        })
    if context.job.status != "running":
        raise ValueError({
            "type": "parent_not_running",
            "code": "parent_not_running",
            "message": f"job {context.job.job_id} is {context.job.status}",
        })
    execution = await spawn_children_detailed(
        db,
        parent_job=context.job,
        children=children,
        daemon_id=context.daemon_id,
        parent_step_id=context.step.step_id,
        idempotency_key=idempotency_key,
        strict=True,
    )
    return execution.response


async def spawn_batches_for_step(
    db: AsyncSession, step_id: str
) -> list[SpawnBatchModel]:
    return list((
        await db.execute(
            select(SpawnBatchModel)
            .where(SpawnBatchModel.parent_step_id == step_id)
            .order_by(
                SpawnBatchModel.created_at.asc(),
                SpawnBatchModel.spawn_id.asc(),
            )
        )
    ).scalars().all())


async def children_for_step(
    db: AsyncSession, step_id: str
) -> list[JobModel]:
    """Return every child Job created by any spawn batch on ``step_id``."""
    batches = await spawn_batches_for_step(db, step_id)
    child_ids: list[str] = []
    for batch in batches:
        try:
            payload = json.loads(batch.result_json)
        except (TypeError, ValueError):
            continue
        for item in payload.get("children", []) if isinstance(payload, dict) else []:
            child_id = item.get("child_job_id") if isinstance(item, dict) else None
            if isinstance(child_id, str) and child_id not in child_ids:
                child_ids.append(child_id)
    if not child_ids:
        return []
    jobs = (
        await db.execute(select(JobModel).where(JobModel.job_id.in_(child_ids)))
    ).scalars().all()
    by_id = {job.job_id: job for job in jobs}
    return [by_id[child_id] for child_id in child_ids if child_id in by_id]


__all__ = [
    "SPAWN_TOOL_DESCRIPTION",
    "SpawnChildInput",
    "SpawnContext",
    "SpawnExecution",
    "resolve_spawn_context",
    "spawn_children_detailed",
    "spawn_children_for_token",
    "spawn_batches_for_step",
    "children_for_step",
]
