"""Pydantic schemas for request/response validation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from ..a2a import Message


# ── Runtime ─────────────────────────────────────────────────────────


class RuntimeInfo(BaseModel):
    """A single runtime registered by a daemon."""

    name: str
    type: str = Field(description="provider key, e.g. 'claude'")
    version: str
    status: str = "online"
    device_name: str


class RegisterRequest(BaseModel):
    daemon_id: str
    # Phase 4: human-readable daemon label. Optional (legacy daemons don't
    # send it); stored on each runtime row alongside daemon_id.
    daemon_name: str | None = None
    runtimes: list[RuntimeInfo]


class RegisterResponse(BaseModel):
    registered: int


class RuntimeOut(RuntimeInfo):
    daemon_id: str
    daemon_name: str | None = None


class ListRuntimesResponse(BaseModel):
    runtimes: list[RuntimeOut]


# ── Task ────────────────────────────────────────────────────────────


class SubmitTaskRequest(BaseModel):
    prompt: str
    # Phase 2: structured (A2A-shaped) input. When set, ``prompt`` is
    # treated as the rendered fallback; when omitted, behavior is
    # byte-identical to the legacy prompt-only path.
    message: Message | None = None
    provider: str = "claude"
    task_id: str | None = None  # if provided, server resumes that task's session
    # Step 3.6: pin this task to a specific daemon. None = free task.
    target_daemon_id: str | None = None
    # Phase 4 workdir: AGT metadata directory, derived from the user's cwd.
    # Server persists it ONLY for local daemons; remote daemons supply
    # their own fallback path on the receiving side.
    work_dir: str | None = None
    # Phase 4 Step 4: identity for this task. When set, the server runs
    # ``select_runtime`` to resolve the actual (provider, daemon_id) and
    # rejects the submit if the identity isn't bound to that runtime.
    identity_did: str | None = None
    # cwd injection (Phase 4 workdir supplement): the agent subprocess's
    # working directory on the daemon machine. Also where IDENTITY.md /
    # AGENTS.md are injected when an identity is set. NULL means "let
    # the daemon pick" (legacy: ``work_dir_root / provider / task_id``).
    agent_cwd: str | None = None


class SubmitTaskResponse(BaseModel):
    task_id: str
    status: str


class ClaimRequest(BaseModel):
    daemon_id: str
    max_concurrent: int = 1
    timeout_seconds: int = 30
    # Step 3.4: list of providers this daemon can run. Server filters
    # pending tasks to those whose provider is in this list. Empty list
    # → match nothing (daemon must declare its capabilities first).
    runtime_ids: list[str] = []


class ClaimResponse(BaseModel):
    tasks: list["TaskPayload"]


class TaskPayload(BaseModel):
    """A task handed off from server to daemon for execution."""

    task_id: str
    provider: str
    prompt: str
    resume_session_id: str | None = None
    work_dir: str | None = None
    # Phase 4 Step 4: identity for this task. When set, daemon uses it to
    # inject the identity's AGENTS.md into the agent's working directory
    # (see identity-inject-reclaim spec). None means identity-less task
    # (legacy behavior).
    identity_did: str | None = None
    # Option D (reverse-dial aware): server-rendered AGENTS.md body for
    # ``identity_did``. The daemon writes this verbatim into
    # ``work_dir/IDENTITY.md`` so it never needs to fetch from the
    # Server. ``None`` when ``identity_did is None`` or the identity has
    # been deleted since submit. Always-fresh: re-rendered on every
    # claim so an updated identity propagates within one claim poll
    # (~500ms) of a new task being submitted.
    identity_md: str | None = None
    # cwd injection (Phase 4 workdir supplement): agent subprocess cwd
    # on the daemon. NULL = daemon picks (``work_dir_root / provider /
    # task_id`` legacy fallback).
    agent_cwd: str | None = None
    # Phase 2: the Step's structured input (Message.model_dump). FileParts
    # may carry server-injected resolution hints: ``inline_b64`` for small
    # content, or ``local_path`` when the claiming daemon IS the producer
    # (same-daemon zero-copy). ``None`` = legacy prompt-only task.
    message: dict | None = None
    # Phase 3: server-computed artifact whitelist subdirectory (relative
    # to ``agent_cwd``). Child jobs get ``.agt/outputs/<job_id>/`` so
    # parallel siblings never cross-attribute files; root jobs get None
    # (shared ``.agt/outputs/``, Phase 2 behavior). The daemon consumes
    # this verbatim — no lineage knowledge on its side (design D9).
    outputs_subdir: str | None = None


class ReportRequest(BaseModel):
    daemon_id: str
    task_id: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float
    status: str  # "completed" | "failed" | "timeout" | "cancelled"
    session_id: str | None = None
    resume_rejected: bool = False
    # Phase 1: structured outcome reported by the daemon. Server derives
    # from exit_code when omitted for backward compatibility.
    outcome: str | None = None
    # Phase 2: declared deliverables scanned from ``.agt/outputs/``.
    # Absent for legacy daemons — behavior is then byte-identical to
    # Phase 1 (the field is purely additive).
    artifacts: list["ArtifactReportItem"] = []
    # Phase 3: structured-outcome sidecar (e.g. spawn intent envelopes
    # ``{"children": [...]}``). Absent for legacy daemons — pure addition.
    outcome_payload: dict | None = None


class ReportResponse(BaseModel):
    recorded: bool
    # Phase 2: ids assigned to the registered artifacts, in report order.
    artifacts: list[str] = []


# ── Artifacts (Phase 2) ─────────────────────────────────────────────


class ArtifactReportItem(BaseModel):
    """One declared deliverable reported by a daemon for a Step.

    ``inline_b64`` carries content <4KB directly in the report; larger
    content stays on the daemon (``local_path``) for lazy cross-daemon
    fetch, or is uploaded eagerly via ``POST /api/artifacts``.
    """

    name: str
    mime_type: str | None = None
    summary: str | None = None
    status_flag: str | None = None
    size: int
    sha256: str
    local_path: str | None = None
    inline_b64: str | None = None


class ArtifactOut(BaseModel):
    """Public artifact metadata. ``uri`` is the only reference form —
    producer-local paths are never exposed."""

    artifact_id: str
    job_id: str
    step_id: str
    producer_identity_id: str | None = None
    name: str
    mime_type: str
    summary: str | None = None
    status_flag: str | None = None
    size: int
    sha256: str
    status: str
    uri: str
    created_at: str


class ArtifactUploadRequest(BaseModel):
    """Eager upload from an HTTP-reachable daemon (pre-report).

    The daemon POSTs one artifact's bytes; the server materializes them
    (inline <4KB / server disk) and returns the registered row. Ownership
    mirrors the report path: only the daemon that claimed the Step may
    upload for it.
    """

    daemon_id: str
    task_id: str
    artifact: ArtifactReportItem
    content_b64: str


class ProgressRequest(BaseModel):
    """Incremental stdout prefix reported mid-execution (Phase 5 remote streaming)."""

    daemon_id: str
    task_id: str
    stdout_prefix: str


class ProgressResponse(BaseModel):
    recorded: bool


class TaskStatusResponse(BaseModel):
    task_id: str
    provider: str
    status: str
    session_id: str | None
    # True when the agent refused to resume a prior session on the last
    # report. Lets the CLI drop a stale cwd-store entry (parity with the
    # local daemon's resume_rejected handling).
    resume_rejected: bool | None = None
    exit_code: int | None
    stdout: str | None = None
    stderr: str | None = None
    duration: float | None
    created_at: str
    updated_at: str
    daemon_id: str | None = None
    target_daemon_id: str | None = None
    # Phase 4 Step 4: identity for this task (None = no-identity task).
    identity_did: str | None = None
    work_dir: str | None = None  # Phase 4: AGT metadata directory (None = legacy / remote)
    # cwd injection (Phase 4 workdir supplement): the agent subprocess's
    # working directory on the daemon machine. None = daemon's default.
    agent_cwd: str | None = None
    # Phase 2: declared deliverables of this Step (public metadata only).
    artifacts: list[ArtifactOut] = []


# ── Stream (SSE) events — Phase 5 remote streaming ──────────────────
#
# The ``event:`` line of the SSE frame carries the same value as ``type``;
# the ``data:`` payload also embeds it so a client that only parses
# ``data`` stays unambiguous.


class StreamSnapshotEvent(BaseModel):
    """Full cumulative stdout + status, sent immediately on subscribe."""

    type: str = "snapshot"
    status: str
    stdout: str


class StreamDeltaEvent(BaseModel):
    """Incremental chunk not yet seen by this subscriber."""

    type: str = "delta"
    chunk: str


class StreamDoneEvent(BaseModel):
    """Authoritative terminal payload; stream closes after this frame."""

    type: str = "done"
    status: str
    stdout: str
    session_id: str | None = None
    exit_code: int | None = None
    resume_rejected: bool | None = None
    stderr: str | None = None
    # Phase 2: declared deliverables of this Step.
    artifacts: list[ArtifactOut] = []


# ── Session ─────────────────────────────────────────────────────────


class SessionOut(BaseModel):
    job_id: str
    # Phase 4: "" for identity-less (legacy) sessions.
    identity_did: str = ""
    provider: str
    session_id: str


class DaemonSessionOut(BaseModel):
    """One conversation session as seen from a daemon-level listing.

    ``dir`` is the agent subprocess's working directory on the daemon
    machine (``agent_cwd``, falling back to ``work_dir``); it is the
    directory the session actually ran in.
    """

    session_id: str
    task_id: str
    provider: str
    identity_did: str | None = None
    dir: str | None = None
    first_prompt: str
    last_seen_at: str


class ListDaemonSessionsResponse(BaseModel):
    """Response listing a daemon's conversation sessions."""

    sessions: list[DaemonSessionOut]


class SessionResolveOut(BaseModel):
    """Reverse lookup of one session_id → job / provider / identity.

    Backs remote ``agt chat --resume <sid> --daemon=X``: the CLI recovers
    provider + identity and obtains ``job_id`` so it can submit subsequent
    turns to ``POST /api/jobs/{job_id}/input``.
    """

    job_id: str
    task_id: str
    session_id: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    dir: str | None = None


class SessionHistoryMessage(BaseModel):
    """One message in a provider session transcript (remote history echo)."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float | None = None


class SessionHistoryOut(BaseModel):
    """Transcript of a remote session, read from the owning daemon.

    ``not_implemented`` is True when the daemon's history reader has no
    support for this session's provider (e.g. cursor) — the CLI then shows
    the same "not implemented" hint it does for local resumes.
    """

    session_id: str
    messages: list[SessionHistoryMessage]
    not_implemented: bool = False


# ── Heartbeat ───────────────────────────────────────────────────────


class HeartbeatRequest(BaseModel):
    daemon_id: str
    active_task_count: int = 0
    # Optional runtime snapshot. When present, the server upserts this
    # daemon's runtimes so the registry stays in sync with whatever the
    # daemon's probe currently sees (default = server leaves runtimes
    # alone, preserving backward compatibility with old clients).
    runtimes: list[RuntimeInfo] = []
    # The daemon's human-readable label. The heartbeat upsert replaces
    # the daemon's rows, so clients that know their name send it every
    # beat; legacy clients omit it and the server preserves the stored
    # value instead of NULLing it.
    daemon_name: str | None = None


class HeartbeatResponse(BaseModel):
    ok: bool
    server_time: str


# ── Health ──────────────────────────────────────────────────────────


class HealthResponse(BaseModel):
    status: str = "ok"


# ── Identity (Phase 4 Step 1) ───────────────────────────────────────


class IdentityCreate(BaseModel):
    """Request to create a new identity."""

    daemon_id: str
    name: str
    slug: str | None = None  # If not provided, derived from name
    description: str = ""
    capabilities: list[str] = []
    visibility: str = "private"
    created_by: str = "user"


class IdentityOut(BaseModel):
    """Full identity response."""

    did: str
    daemon_id: str
    name: str
    description: str
    capabilities: list[str]
    status: str
    visibility: str
    created_by: str | None
    created_at: str
    updated_at: str


class IdentityUpdate(BaseModel):
    """Partial update request for an identity."""

    name: str | None = None
    description: str | None = None
    add_capabilities: list[str] = []
    remove_capabilities: list[str] = []


class IdentityBindingCreate(BaseModel):
    """Request to create a binding between identity and runtime."""

    daemon_id: str
    provider: str
    priority: int = 0
    is_default: bool = False


class IdentityBindingOut(BaseModel):
    """Binding response."""

    id: int
    identity_did: str
    daemon_id: str
    provider: str
    priority: int
    is_default: bool
    created_at: str


class ListIdentitiesResponse(BaseModel):
    """Response listing identities."""

    identities: list[IdentityOut]


class ListBindingsResponse(BaseModel):
    """Response listing an identity's bindings."""

    bindings: list[IdentityBindingOut]


class ResolveRequest(BaseModel):
    """Dry-run selection: which runtime would this identity run on?

    ``identity_did`` is required — callers decide themselves whether to
    take the no-identity legacy path (there is no daemon-level default
    identity; see phase4-design.md §12.1). It is optional at the Pydantic
    layer only so the route can answer a plain 400 (not FastAPI's 422)
    when it is missing.
    """

    identity_did: str | None = None
    runtime_hint: str | None = None
    daemon_id: str = "local"


class DaemonProviderOut(BaseModel):
    """One runtime row as seen inside a daemon's provider list."""

    provider: str
    name: str
    version: str
    status: str
    last_seen: str


class DaemonOut(BaseModel):
    """Aggregated daemon summary (grouped from runtimes rows)."""

    daemon_id: str
    daemon_name: str | None = None
    device_name: str
    providers: list[DaemonProviderOut]
    runtime_count: int
    last_seen: str
    online: bool


class ListDaemonsResponse(BaseModel):
    """Response listing all known daemons."""

    daemons: list[DaemonOut]


class ResolveResponse(BaseModel):
    """Selection result for a dry-run resolve."""

    daemon_id: str
    provider: str
    identity_did: str


ClaimResponse.model_rebuild()
__all__ = [
    "RuntimeInfo",
    "RegisterRequest",
    "RegisterResponse",
    "RuntimeOut",
    "ListRuntimesResponse",
    "SubmitTaskRequest",
    "SubmitTaskResponse",
    "ClaimRequest",
    "ClaimResponse",
    "TaskPayload",
    "ReportRequest",
    "ReportResponse",
    "ProgressRequest",
    "ProgressResponse",
    "TaskStatusResponse",
    "StreamSnapshotEvent",
    "StreamDeltaEvent",
    "StreamDoneEvent",
    "SessionOut",
    "DaemonSessionOut",
    "ListDaemonSessionsResponse",
    "SessionResolveOut",
    "SessionHistoryMessage",
    "SessionHistoryOut",
    "HeartbeatRequest",
    "HeartbeatResponse",
    "HealthResponse",
    "IdentityCreate",
    "IdentityOut",
    "IdentityUpdate",
    "IdentityBindingCreate",
    "IdentityBindingOut",
    "ListIdentitiesResponse",
    "ListBindingsResponse",
    "ResolveRequest",
    "ResolveResponse",
    "DaemonProviderOut",
    "DaemonOut",
    "ListDaemonsResponse",
]
