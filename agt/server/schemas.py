"""Pydantic schemas for request/response validation."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
    runtimes: list[RuntimeInfo]


class RegisterResponse(BaseModel):
    registered: int


class RuntimeOut(RuntimeInfo):
    daemon_id: str


class ListRuntimesResponse(BaseModel):
    runtimes: list[RuntimeOut]


# ── Task ────────────────────────────────────────────────────────────


class SubmitTaskRequest(BaseModel):
    prompt: str
    provider: str = "claude"
    task_id: str | None = None  # if provided, server resumes that task's session
    # Step 3.6: pin this task to a specific daemon. None = free task.
    target_daemon_id: str | None = None
    # Phase 4 workdir: AGT metadata directory, derived from the user's cwd.
    # Server persists it ONLY for local daemons; remote daemons supply
    # their own fallback path on the receiving side.
    work_dir: str | None = None


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


class ReportResponse(BaseModel):
    recorded: bool


class TaskStatusResponse(BaseModel):
    task_id: str
    provider: str
    status: str
    session_id: str | None
    exit_code: int | None
    stdout: str | None = None
    stderr: str | None = None
    duration: float | None
    created_at: str
    updated_at: str
    daemon_id: str | None = None
    target_daemon_id: str | None = None
    work_dir: str | None = None  # Phase 4: AGT metadata directory (None = legacy / remote)


# ── Session ─────────────────────────────────────────────────────────


class SessionOut(BaseModel):
    task_id: str
    provider: str
    session_id: str


# ── Heartbeat ───────────────────────────────────────────────────────


class HeartbeatRequest(BaseModel):
    daemon_id: str
    active_task_count: int = 0
    # Optional runtime snapshot. When present, the server upserts this
    # daemon's runtimes so the registry stays in sync with whatever the
    # daemon's probe currently sees (default = server leaves runtimes
    # alone, preserving backward compatibility with old clients).
    runtimes: list[RuntimeInfo] = []


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
    "TaskStatusResponse",
    "SessionOut",
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