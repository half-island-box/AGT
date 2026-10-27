"""Goal-mode shared context storage, visibility, and Brief projection."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import FilePart, parse_artifact_uri
from ..models import (
    ArtifactModel,
    ContextEntryModel,
    JobModel,
    StepModel,
)
from .auth_service import verify_task_token_claims


MAX_CONTEXT_ENTRY_BYTES = 4096
MAX_CONTEXT_WRITES_PER_STEP = 8
MAX_ACTIVE_CONTEXT_ENTRIES_PER_JOB = 32
MAX_ACTIVE_CONTEXT_ENTRIES_PER_ROOT = 512
MAX_CONTEXT_HISTORY_ENTRIES_PER_ROOT = 2048
MAX_CONTEXT_REFS = 16
MAX_CONTEXT_BRIEF_BYTES = 4096
MAX_CONTEXT_READ_BYTES = 4096
BRIEF_SCHEMA_VERSION = 1
CONTEXT_WRITE_TOOL_DESCRIPTION = (
    "Write or retract Goal shared context. The server derives the caller "
    "and canonical key from the task token; agent-supplied root ids are "
    "ignored. Updating an existing key requires expected_head_entry_id."
)
CONTEXT_READ_TOOL_DESCRIPTION = (
    "Read active Goal shared context visible to the current Job. Results "
    "are paginated and always re-filtered by the task token."
)

SCOPES = {"shared", "job"}
KINDS = {"decision", "constraint", "fact", "note"}
ENTRY_STATUSES = {"active", "superseded", "retracted"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._/-]{0,127}$")
_SECRET_RE = re.compile(
    r"(?i)(api[_-]?key|bearer\s+[a-z0-9._-]+|private\s+key|password\s*=|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|sk-[a-z0-9_-]{16,}|"
    r"gh[pousr]_[a-z0-9]{20,}|xox[baprs]-[a-z0-9-]{10,}|"
    r"aws_secret_access_key)"
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _err(code: str, message: str, **extra: Any) -> ValueError:
    return ValueError({"type": code, "code": code, "message": message, **extra})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass(frozen=True)
class ContextCaller:
    step: StepModel
    job: JobModel
    daemon_id: str


async def resolve_context_caller(
    db: AsyncSession,
    *,
    task_token: str | None = None,
    claims: dict | None = None,
    trusted_daemon_id: str | None = None,
    secret: str = "",
    require_running: bool = True,
) -> ContextCaller:
    """Resolve and authorize the Step that owns a context operation."""
    if task_token:
        if not secret:
            raise _err("context_unavailable", "server secret is not configured")
        try:
            claims = verify_task_token_claims(task_token, secret)
        except PermissionError:
            raise _err("unauthenticated", "invalid task token") from None
    if not isinstance(claims, dict):
        raise _err("unauthenticated", "task token required")

    subject = claims.get("sub")
    if not isinstance(subject, str) or not subject.startswith("agent:"):
        raise _err("unauthenticated", "token is not agent scoped")
    daemon_id = subject.removeprefix("agent:")
    if trusted_daemon_id is not None and daemon_id != trusted_daemon_id:
        raise _err("task_not_owned", "task token belongs to another daemon")

    step_id = claims.get("task_id")
    step = await db.get(StepModel, step_id)
    if step is None or step.job_id is None:
        raise _err("task_not_found", "task not found")
    if step.daemon_id != daemon_id:
        raise _err("task_not_owned", "task token does not own this Step")
    if require_running and step.status != "running":
        raise _err("step_not_running", f"step {step_id} is {step.status}")

    job = await db.get(JobModel, step.job_id)
    if job is None:
        raise _err("job_not_found", "job not found")
    if job.job_type != "goal":
        raise _err("not_goal_job", f"job {job.job_id} is {job.job_type}")
    return ContextCaller(step=step, job=job, daemon_id=daemon_id)


async def _lineage_ids(db: AsyncSession, root_job_id: str, job_id: str) -> set[str]:
    rows = (
        await db.execute(select(JobModel).where(JobModel.root_job_id == root_job_id))
    ).scalars().all()
    parents = {row.job_id: row.parent_job_id for row in rows}
    ancestors: set[str] = set()
    current: str | None = job_id
    while current is not None and current not in ancestors:
        ancestors.add(current)
        current = parents.get(current)

    children: dict[str | None, set[str]] = {}
    for child, parent in parents.items():
        children.setdefault(parent, set()).add(child)
    descendants: set[str] = set()
    frontier = {job_id}
    while frontier:
        next_frontier: set[str] = set()
        for candidate in frontier:
            next_frontier.update(children.get(candidate, set()) - descendants)
        descendants.update(next_frontier)
        frontier = next_frontier
    return ancestors | descendants


async def visible_context_entries(
    db: AsyncSession, *, root_job_id: str, job_id: str
) -> list[ContextEntryModel]:
    lineage = await _lineage_ids(db, root_job_id, job_id)
    rows = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.root_job_id == root_job_id,
                ContextEntryModel.status == "active",
            ).order_by(ContextEntryModel.entry_id.desc())
        )
    ).scalars().all()
    return [
        row
        for row in rows
        if row.scope == "shared" or row.author_job_id in lineage
    ]


def _validated_content(content: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(content, dict):
        raise _err("invalid_context_content", "content must be an object")
    allowed = {"text", "data", "summary", "artifact_refs"}
    unknown = set(content) - allowed
    if unknown:
        raise _err(
            "invalid_context_content",
            f"unknown content fields: {', '.join(sorted(unknown))}",
        )

    text = content.get("text")
    summary = content.get("summary")
    data = content.get("data", {})
    refs = content.get("artifact_refs", [])
    if not any([text, data, summary, refs]):
        raise _err("invalid_context_content", "content must not be empty")
    if text is not None and not isinstance(text, str):
        raise _err("invalid_context_content", "text must be a string")
    if summary is not None and (
        not isinstance(summary, str) or len(summary) > 256
    ):
        raise _err("invalid_context_content", "summary must be at most 256 chars")
    if not isinstance(data, dict):
        raise _err("invalid_context_content", "data must be an object")
    if _max_nested_depth(data) > 4:
        raise _err("invalid_context_content", "data depth must be <= 4")
    if not isinstance(refs, list) or any(not isinstance(item, str) for item in refs):
        raise _err("invalid_context_content", "artifact_refs must be string list")

    serialized = _canonical_json(content).encode("utf-8")
    if len(serialized) > MAX_CONTEXT_ENTRY_BYTES:
        raise _err(
            "context_entry_too_large",
            f"context entry exceeds {MAX_CONTEXT_ENTRY_BYTES} bytes; use an Artifact",
        )
    rendered = f"{text or ''}\n{summary or ''}\n{_canonical_json(data)}"
    if _SECRET_RE.search(rendered):
        raise _err("context_secret_rejected", "content appears to contain a secret")
    return content


def _max_nested_depth(value: Any) -> int:
    if isinstance(value, dict):
        return 1 + max((_max_nested_depth(child) for child in value.values()), default=0)
    if isinstance(value, list):
        return 1 + max((_max_nested_depth(child) for child in value), default=0)
    return 0


def _canonical_key(
    *, scope: str, slug: str, author_job_id: str, root_job_id: str
) -> str:
    if scope not in SCOPES:
        raise _err("invalid_context_scope", f"scope must be one of {sorted(SCOPES)}")
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise _err("invalid_context_key", "invalid context key slug")
    if any(part in {"", ".", ".."} for part in slug.split("/")):
        raise _err("invalid_context_key", "context key contains an invalid path segment")
    if scope == "job":
        if author_job_id == root_job_id:
            raise _err("invalid_context_scope", "root must use shared scope")
        return f"job/{author_job_id}/{slug}"
    if author_job_id == root_job_id:
        return f"shared/{slug}"
    return f"shared/proposal/{author_job_id}/{slug}"


def _validate_kind_for_scope(kind: str, *, scope: str, is_root: bool) -> None:
    if kind not in KINDS:
        raise _err("invalid_context_kind", f"kind must be one of {sorted(KINDS)}")
    if scope == "shared" and not is_root and kind not in {"fact", "note"}:
        raise _err(
            "context_permission_denied",
            "non-root agents may only publish shared facts or notes",
        )


async def _artifact_refs_visible(
    db: AsyncSession,
    *,
    refs: list[str],
    root_job_id: str,
    job_id: str,
) -> None:
    if not refs:
        return
    lineage = await _lineage_ids(db, root_job_id, job_id)
    artifact_ids = [parse_artifact_uri(ref) for ref in refs]
    if any(item is None for item in artifact_ids):
        raise _err("invalid_artifact_ref", "invalid agt-artifact URI")
    for artifact_id in artifact_ids:
        artifact = await db.get(ArtifactModel, artifact_id)
        if artifact is None:
            raise _err("artifact_not_found", f"artifact {artifact_id} not found")
        artifact_job = await db.get(JobModel, artifact.job_id)
        if (
            artifact_job is None
            or artifact_job.root_job_id != root_job_id
            or artifact.job_id not in lineage
        ):
            raise _err(
                "artifact_not_visible",
                f"artifact {artifact_id} is not visible to this Job",
            )


async def _check_quotas(
    db: AsyncSession, *, caller: ContextCaller, creating_active: bool
) -> None:
    root_rows = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.root_job_id == caller.job.root_job_id
            )
        )
    ).scalars().all()
    if len(root_rows) >= MAX_CONTEXT_HISTORY_ENTRIES_PER_ROOT:
        raise _err(
            "context_history_quota_exhausted",
            f"context history exceeds {MAX_CONTEXT_HISTORY_ENTRIES_PER_ROOT}",
        )
    if not creating_active:
        return
    if sum(row.status == "active" for row in root_rows) >= MAX_ACTIVE_CONTEXT_ENTRIES_PER_ROOT:
        raise _err(
            "context_active_quota_exhausted",
            f"active context entries exceed {MAX_ACTIVE_CONTEXT_ENTRIES_PER_ROOT}",
        )
    if (
        sum(
            row.status == "active" and row.author_job_id == caller.job.job_id
            for row in root_rows
        )
        >= MAX_ACTIVE_CONTEXT_ENTRIES_PER_JOB
    ):
        raise _err(
            "context_job_quota_exhausted",
            f"active context entries for this Job exceed "
            f"{MAX_ACTIVE_CONTEXT_ENTRIES_PER_JOB}",
        )


async def context_write_for_caller(
    db: AsyncSession,
    caller: ContextCaller,
    *,
    scope: str,
    key: str,
    kind: str,
    content: dict[str, Any],
    idempotency_key: str,
    expected_head_entry_id: int | None = None,
) -> dict[str, Any]:
    if caller.job.status in {"done", "failed", "cancelled"}:
        raise _err("root_not_active", "root Job is terminal")
    if not isinstance(idempotency_key, str) or not idempotency_key or len(idempotency_key) > 128:
        raise _err("invalid_idempotency_key", "idempotency_key is invalid")

    prior_idem = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.author_step_id == caller.step.step_id,
                ContextEntryModel.idempotency_key == idempotency_key,
            )
        )
    ).scalars().first()
    if prior_idem is not None:
        return {
            "ok": True,
            "entry_id": prior_idem.entry_id,
            "key": prior_idem.key,
            "status": prior_idem.status,
            "head_entry_id": prior_idem.entry_id,
            "duplicate": True,
        }

    step_rows = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.author_step_id == caller.step.step_id
            )
        )
    ).scalars().all()
    if len(step_rows) >= MAX_CONTEXT_WRITES_PER_STEP:
        raise _err(
            "context_step_quota_exhausted",
            f"context writes for this Step exceed {MAX_CONTEXT_WRITES_PER_STEP}",
        )

    content = _validated_content(content)
    canonical = _canonical_key(
        scope=scope,
        slug=key,
        author_job_id=caller.job.job_id,
        root_job_id=caller.job.root_job_id,
    )
    _validate_kind_for_scope(
        kind, scope=scope, is_root=caller.job.job_id == caller.job.root_job_id
    )
    await _artifact_refs_visible(
        db,
        refs=content.get("artifact_refs", []),
        root_job_id=caller.job.root_job_id,
        job_id=caller.job.job_id,
    )

    head = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.root_job_id == caller.job.root_job_id,
                ContextEntryModel.key == canonical,
                ContextEntryModel.status == "active",
            )
        )
    ).scalars().first()
    if head is not None:
        if expected_head_entry_id is None:
            raise _err(
                "context_head_conflict",
                "expected_head_entry_id is required",
                key=canonical,
                current_head_entry_id=head.entry_id,
            )
        if expected_head_entry_id != head.entry_id:
            raise _err(
                "context_head_conflict",
                "context head changed",
                key=canonical,
                current_head_entry_id=head.entry_id,
            )
        if head.kind != kind:
            raise _err(
                "context_kind_mismatch",
                "context kind cannot change",
                key=canonical,
            )

    await _check_quotas(db, caller=caller, creating_active=head is None)
    entry = ContextEntryModel(
        root_job_id=caller.job.root_job_id,
        author_job_id=caller.job.job_id,
        author_step_id=caller.step.step_id,
        scope=scope,
        key=canonical,
        kind=kind,
        content_json=_canonical_json(content),
        status="active",
        idempotency_key=idempotency_key,
        created_at=_now(),
    )
    try:
        async with db.begin_nested():
            if head is not None:
                head.status = "superseded"
            db.add(entry)
            await db.flush()
    except IntegrityError:
        raise _err(
            "context_head_conflict",
            "context head changed concurrently",
            key=canonical,
        ) from None
    return {
        "ok": True,
        "entry_id": entry.entry_id,
        "key": entry.key,
        "status": entry.status,
        "head_entry_id": entry.entry_id,
        "duplicate": False,
    }


async def context_retract_for_caller(
    db: AsyncSession,
    caller: ContextCaller,
    *,
    key: str,
    expected_head_entry_id: int,
    idempotency_key: str,
) -> dict[str, Any]:
    if caller.job.status in {"done", "failed", "cancelled"}:
        raise _err("root_not_active", "root Job is terminal")
    head = (
        await db.execute(
            select(ContextEntryModel).where(
                ContextEntryModel.root_job_id == caller.job.root_job_id,
                ContextEntryModel.key == key,
                ContextEntryModel.status == "active",
            )
        )
    ).scalars().first()
    if head is None:
        raise _err("context_head_not_found", f"active context head not found: {key}")
    if head.entry_id != expected_head_entry_id:
        raise _err(
            "context_head_conflict",
            "context head changed",
            key=key,
            current_head_entry_id=head.entry_id,
        )
    is_root = caller.job.job_id == caller.job.root_job_id
    if not is_root and head.author_job_id != caller.job.job_id:
        raise _err("context_permission_denied", "only the author or root may retract")
    head.status = "retracted"
    await db.flush()
    return {"ok": True, "entry_id": head.entry_id, "status": head.status}


async def context_read_for_caller(
    db: AsyncSession,
    caller: ContextCaller,
    *,
    keys: list[str] | None = None,
    scopes: list[str] | None = None,
    kinds: list[str] | None = None,
    entry_id_after: int | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    rows = await visible_context_entries(
        db,
        root_job_id=caller.job.root_job_id,
        job_id=caller.job.job_id,
    )
    if keys is not None:
        wanted = set(keys)
        rows = [row for row in rows if row.key in wanted]
    if scopes is not None:
        wanted = set(scopes)
        rows = [row for row in rows if row.scope in wanted]
    if kinds is not None:
        wanted = set(kinds)
        rows = [row for row in rows if row.kind in wanted]
    if entry_id_after is not None:
        rows = [row for row in rows if row.entry_id > entry_id_after]

    max_items = min(max(limit or 20, 1), 50)
    selected: list[dict[str, Any]] = []
    used = 0
    next_cursor: int | None = None
    for row in rows:
        item = _entry_out(row)
        size = len(_canonical_json(item).encode("utf-8"))
        if len(selected) >= max_items or used + size > MAX_CONTEXT_READ_BYTES:
            next_cursor = row.entry_id - 1
            break
        selected.append(item)
        used += size
    return {
        "entries": selected,
        "omitted_count": max(0, len(rows) - len(selected)),
        "next_cursor": next_cursor,
    }


def _entry_out(row: ContextEntryModel) -> dict[str, Any]:
    return {
        "entry_id": row.entry_id,
        "key": row.key,
        "scope": row.scope,
        "kind": row.kind,
        "status": row.status,
        "author_job_id": row.author_job_id,
        "content": json.loads(row.content_json),
        "created_at": row.created_at,
    }


async def apply_context_ops(
    db: AsyncSession,
    *,
    step: StepModel,
    daemon_id: str,
    ops: list[dict[str, Any]],
    report_status: str,
) -> list[dict[str, Any]]:
    """Apply marker operations captured while an agent subprocess ran."""
    if not ops:
        return []
    if daemon_id != step.daemon_id:
        raise _err("task_not_owned", "report daemon does not own this Step")
    if report_status != "completed" or len(ops) > MAX_CONTEXT_WRITES_PER_STEP:
        return [
            {"ok": False, "error": "context marker rejected for report status/quota"}
        ]
    job = await db.get(JobModel, step.job_id)
    if job is None:
        raise _err("job_not_found", "job not found")
    caller = ContextCaller(step=step, job=job, daemon_id=daemon_id)
    results: list[dict[str, Any]] = []
    for op in ops:
        try:
            if op.get("op") == "write":
                result = await context_write_for_caller(
                    db,
                    caller,
                    scope=str(op.get("scope", "")),
                    key=str(op.get("key", "")),
                    kind=str(op.get("kind", "")),
                    content=op.get("content", {}),
                    idempotency_key=str(op.get("idempotency_key", "")),
                    expected_head_entry_id=op.get("expected_head_entry_id"),
                )
            elif op.get("op") == "retract":
                result = await context_retract_for_caller(
                    db,
                    caller,
                    key=str(op.get("key", "")),
                    expected_head_entry_id=int(op.get("expected_head_entry_id", 0)),
                    idempotency_key=str(op.get("idempotency_key", "")),
                )
            else:
                raise _err("invalid_context_op", "unsupported context op")
            results.append(result)
        except ValueError as exc:
            payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
            results.append({
                "ok": False,
                "error": payload.get("message", str(exc)),
                "code": payload.get("code", "context_failed"),
            })
    return results


def _input_context_refs(step: StepModel) -> list[str]:
    if not step.input_message:
        return []
    try:
        raw = json.loads(step.input_message)
    except (TypeError, ValueError):
        return []
    refs: list[str] = []
    for part in raw.get("parts", []):
        if part.get("type") != "data":
            continue
        candidate = part.get("data", {}).get("context_refs", [])
        if isinstance(candidate, list):
            refs.extend(item for item in candidate if isinstance(item, str))
    return refs[:MAX_CONTEXT_REFS]


def _brief_entry(row: ContextEntryModel, *, summary_only: bool = False) -> dict[str, Any]:
    content = json.loads(row.content_json)
    if summary_only and content.get("summary"):
        content = {
            "summary": content["summary"],
            "data": content.get("data", {}),
            "artifact_refs": content.get("artifact_refs", []),
        }
    return {
        "entry_id": row.entry_id,
        "key": row.key,
        "kind": row.kind,
        "content": content,
    }


def _auto_selected(
    rows: list[ContextEntryModel], refs: set[str], root_job_id: str
) -> list[ContextEntryModel]:
    selected: list[ContextEntryModel] = []
    for row in rows:
        is_root_shared = (
            row.scope == "shared"
            and row.author_job_id == root_job_id
            and not row.key.startswith("shared/proposal/")
        )
        if row.key in refs or (
            is_root_shared and row.kind in {"decision", "constraint"}
        ) or (
            row.scope == "job" and row.kind in {"decision", "constraint"}
        ):
            selected.append(row)
    return selected


def _sorted_brief_rows(
    rows: list[ContextEntryModel], refs: set[str], root_job_id: str
) -> list[ContextEntryModel]:
    def rank(row: ContextEntryModel) -> tuple[int, int]:
        if row.key in refs:
            priority = 0
        elif (
            row.scope == "shared"
            and row.author_job_id == root_job_id
            and row.kind in {"decision", "constraint"}
        ):
            priority = 1
        elif row.scope == "job" and row.kind == "constraint":
            priority = 2
        else:
            priority = 3
        return priority, -row.entry_id

    return sorted(rows, key=rank)


async def build_context_brief(
    db: AsyncSession,
    *,
    step: StepModel,
    job: JobModel,
) -> tuple[dict[str, Any], list[FilePart]] | None:
    """Build a full-only Brief and its separately resolvable Artifact refs."""
    rows = await visible_context_entries(
        db, root_job_id=job.root_job_id, job_id=job.job_id
    )
    refs = set(_input_context_refs(step))
    selected = _auto_selected(rows, refs, job.root_job_id)
    ordered = _sorted_brief_rows(selected, refs, job.root_job_id)

    lineage = await _lineage_ids(db, job.root_job_id, job.job_id)
    entries: list[dict[str, Any]] = []
    used = 0
    artifact_uris: list[str] = []
    for row in ordered:
        item = _brief_entry(row)
        refs_to_keep: list[str] = []
        for uri in item.get("content", {}).get("artifact_refs", []):
            artifact_id = parse_artifact_uri(uri)
            artifact = await db.get(ArtifactModel, artifact_id) if artifact_id else None
            artifact_job = await db.get(JobModel, artifact.job_id) if artifact else None
            if (
                artifact is not None
                and artifact_job is not None
                and artifact_job.root_job_id == job.root_job_id
                and artifact.job_id in lineage
            ):
                refs_to_keep.append(uri)
        if item.get("content", {}).get("artifact_refs"):
            item["content"]["artifact_refs"] = refs_to_keep
        size = len(_canonical_json(item).encode("utf-8"))
        if used + size > MAX_CONTEXT_BRIEF_BYTES:
            item = _brief_entry(row, summary_only=True)
            size = len(_canonical_json(item).encode("utf-8"))
        if used + size > MAX_CONTEXT_BRIEF_BYTES:
            continue
        entries.append(item)
        used += size
        artifact_uris.extend(item.get("content", {}).get("artifact_refs", []))

    if not entries:
        return None
    omitted = max(0, len(rows) - len(entries))
    watermark = max((row.entry_id for row in rows), default=0)
    core = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "watermark": watermark,
        "entries": entries,
        "omitted_count": omitted,
    }
    snapshot_source = {
        "schema_version": BRIEF_SCHEMA_VERSION,
        "budget_bytes": MAX_CONTEXT_BRIEF_BYTES,
        "entries": entries,
        "omitted_count": omitted,
    }
    brief = {
        **core,
        "snapshot_id": hashlib.sha256(
            _canonical_json(snapshot_source).encode("utf-8")
        ).hexdigest(),
    }
    file_parts = [FilePart(uri=uri) for uri in dict.fromkeys(artifact_uris)]
    return brief, file_parts


async def context_write_for_token(
    db: AsyncSession,
    *,
    task_token: str | None,
    secret: str,
    op: dict[str, Any],
) -> dict[str, Any]:
    try:
        caller = await resolve_context_caller(
            db,
            task_token=task_token,
            secret=secret,
            require_running=True,
        )
        if op.get("op", "write") == "retract":
            return await context_retract_for_caller(
                db,
                caller,
                key=str(op.get("key", "")),
                expected_head_entry_id=int(op.get("expected_head_entry_id", 0)),
                idempotency_key=str(op.get("idempotency_key", "")),
            )
        return await context_write_for_caller(
            db,
            caller,
            scope=str(op.get("scope", "")),
            key=str(op.get("key", "")),
            kind=str(op.get("kind", "")),
            content=op.get("content", {}),
            idempotency_key=str(op.get("idempotency_key", "")),
            expected_head_entry_id=op.get("expected_head_entry_id"),
        )
    except ValueError as exc:
        payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
        return {
            "ok": False,
            "error": {
                "code": payload.get("code", "context_failed"),
                "message": payload.get("message", str(exc)),
            },
        }


__all__ = [
    "ContextCaller",
    "resolve_context_caller",
    "visible_context_entries",
    "context_write_for_caller",
    "context_retract_for_caller",
    "context_read_for_caller",
    "apply_context_ops",
    "build_context_brief",
    "context_write_for_token",
]
