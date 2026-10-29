"""Pydantic schemas for request/response validation."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_serializer

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
    # Identity for this task. When set, the server resolves the actual
    # (provider, daemon_id). Bindings are routing preferences only; the
    # identity may use any matching live runtime.
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
    # Phase 4: unreported cancellations for THIS daemon (HTTP long-poll
    # cancel channel). Steps with status=cancelled and exit_code IS NULL
    # — the daemon kills those subprocesses and reports cancelled, which
    # sets exit_code and drops them from future lists (self-draining).
    cancelled_task_ids: list[str] = []


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


class UsageOut(BaseModel):
    """Token/cost summary for one Step.

    ``scope="turn"`` values come from the provider's terminal aggregate
    (``result.usage``), which is authoritative. Per-call ``message`` usage
    lives only in the event stream and is deliberately NOT summed here.
    Billing input = ``input_tokens + cache_creation + cache_read``; the
    cache counters are exposed separately so a UI can show hit rate.
    """

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    cost_usd: float | None = None
    model: str | None = None
    num_turns: int | None = None


class AgentEvent(BaseModel):
    """One structured agent event (thinking / tool use / tool result / usage).

    ``seq`` is globally monotonic per Step; ``text_offset`` is the length
    of the cumulative plain text when the event was emitted. Kind-specific
    fields (``block_id``, ``tool_use_id``, ``text``, ``input``, ``content``,
    usage counters, …) are kept verbatim via ``extra="allow"`` so a new
    provider event does not require a schema change.
    """

    model_config = ConfigDict(extra="allow")

    seq: int
    kind: str
    text_offset: int = 0


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
    # Shared-context marker operations captured during the subprocess run.
    context_ops: list[dict] = []
    # Turn-level usage summary (``result.usage`` + cost/model/num_turns).
    # Optional: legacy daemons omit it and the Step keeps NULL columns.
    usage: UsageOut | None = None
    # Structured events not acknowledged by a previous progress call (the
    # daemon re-sends its tail here so a lost progress frame never loses
    # an event). Idempotent on ``(task_id, seq)``.
    agent_events: list[AgentEvent] = []


class ReportResponse(BaseModel):
    recorded: bool
    # Phase 2: ids assigned to the registered artifacts, in report order.
    artifacts: list[str] = []
    # Phase 4: machine-readable no-op reason (e.g. "task_cancelled" when
    # a late report hits an already-cancelled Step). Optional — old
    # clients ignore it; new clients get the same value on HTTP and WS.
    reason: str | None = None

    @model_serializer(mode="wrap")
    def _omit_empty_reason(self, handler):
        """Drop ``reason`` when absent so a normal report stays byte-identical
        to the WS path's raw dict (``{"recorded": True, "artifacts": []}``)
        rather than leaking a spurious ``"reason": null``."""
        data = handler(self)
        if data.get("reason") is None:
            data.pop("reason", None)
        return data


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
    # Structured events produced since the previous sample. Empty for
    # legacy daemons; the stdout prefix contract is unchanged.
    events: list[AgentEvent] = []


# ── Input uploads (chat attachments) ────────────────────────────────


class UploadCreateRequest(BaseModel):
    """One user file uploaded before/with a chat turn.

    JSON base64 (no multipart dependency): the bytes are small enough
    for a local bridge and the same shape works from the VS Code webview,
    the browser console and the CLI alike.
    """

    name: str
    mime_type: str | None = None
    content_b64: str
    # Origin hint: "console" / "vscode" / "cli". Display/audit only.
    source: str = ""


class UploadOut(BaseModel):
    """Public upload metadata. ``uri`` is the only reference form."""

    upload_id: str
    name: str
    mime_type: str
    size: int
    sha256: str
    uri: str
    source: str = ""
    created_at: str
    expires_at: str | None = None


class UploadResolveOut(BaseModel):
    """``uploads.get`` RPC response — bytes + display metadata."""

    content_b64: str
    name: str
    mime_type: str = ""
    sha256: str


class ProgressResponse(BaseModel):
    recorded: bool
    # Highest ``seq`` stored for this Step after the write. Only present
    # when the request carried events (legacy responses stay ``{recorded}``).
    last_seq: int | None = None


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
    # Token/cost summary; None for legacy rows / providers without usage.
    usage: UsageOut | None = None


# ── Stream (SSE) events — Phase 5 remote streaming ──────────────────
#
# The ``event:`` line of the SSE frame carries the same value as ``type``;
# the ``data:`` payload also embeds it so a client that only parses
# ``data`` stays unambiguous.


class StreamSnapshotEvent(BaseModel):
    """Full cumulative stdout + status, sent immediately on subscribe.

    ``events`` replays the structured events already stored for this Step
    (so a reconnect never loses thinking/tool history), and ``event_cursor``
    is the highest ``seq`` included — the hub suppresses anything at or
    below it. ``usage`` may be ``None`` while the turn is still running.
    """

    type: str = "snapshot"
    status: str
    stdout: str
    events: list[dict] = []
    event_cursor: int = 0
    usage: UsageOut | None = None


class StreamDeltaEvent(BaseModel):
    """Incremental chunk not yet seen by this subscriber."""

    type: str = "delta"
    chunk: str


class StreamAgentEventsEvent(BaseModel):
    """One batch of structured events not yet seen by this subscriber."""

    type: str = "agent_events"
    events: list[dict] = []


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
    # Token/cost summary and model name for the finished turn.
    usage: UsageOut | None = None
    model: str | None = None


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
    """One message in a provider session transcript."""

    role: str  # "user" | "assistant" | "system"
    content: str
    timestamp: float | None = None


class SessionHistoryOut(BaseModel):
    """Transcript of a session, read from its owning daemon or local store.

    ``not_implemented`` is True when the history reader has no support for
    this session's provider (e.g. cursor) — the CLI then shows the same
    "not implemented" hint it does for local resumes.
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


# ── Console projects ────────────────────────────────────────────────


class ConsoleProjectCreate(BaseModel):
    path: str
    name: str | None = None
    # Optional remote working directory. ``path`` is always the mandatory
    # local directory; these two are the pair "daemon + directory on that
    # daemon" the user optionally picked at creation time.
    remote_daemon_id: str | None = None
    remote_path: str | None = None


class ConsoleProjectUpdate(BaseModel):
    name: str


class ConsoleProjectRunCounts(BaseModel):
    chat: int = 0
    sop: int = 0
    goal: int = 0


class ConsoleProjectOut(BaseModel):
    id: str
    name: str
    path: str
    available: bool
    # Both None for local-only projects; both set when the project also
    # carries a remote working directory.
    remote_daemon_id: str | None = None
    remote_path: str | None = None
    remote: bool = False
    created_at: str
    updated_at: str
    last_opened_at: str | None = None
    run_counts: ConsoleProjectRunCounts = Field(default_factory=ConsoleProjectRunCounts)


class ConsoleProjectListResponse(BaseModel):
    projects: list[ConsoleProjectOut]


class ConsoleDirectoryOut(BaseModel):
    name: str
    path: str


class ConsoleDirectoryListResponse(BaseModel):
    roots: list[str]
    parent: str | None
    current: str
    directories: list[ConsoleDirectoryOut]


class ConsoleDirectoryPickResponse(BaseModel):
    path: str | None = None


class ConsoleSopRenameRequest(BaseModel):
    path: str
    new_name: str


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


class IdentityBindingUpdate(BaseModel):
    """Partial update for an existing binding."""

    priority: int | None = None
    is_default: bool | None = None


class IdentityBindingOut(BaseModel):
    """Binding response."""

    id: int
    identity_did: str
    daemon_id: str
    provider: str
    priority: int
    is_default: bool
    created_at: str


class IdentityBindingOverviewOut(IdentityBindingOut):
    """Binding plus the current runtime liveness hint for settings UI."""

    runtime_online: bool
    last_seen: str | None = None


class IdentityOverviewOut(IdentityOut):
    """Identity with all optional runtime routing preferences."""

    bindings: list[IdentityBindingOverviewOut] = []


class ListIdentitiesResponse(BaseModel):
    """Response listing identities."""

    identities: list[IdentityOut]


class ListIdentitiesOverviewResponse(BaseModel):
    """Identity list with bindings embedded to avoid client-side N+1 reads."""

    identities: list[IdentityOverviewOut]


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
    daemon_id: str | None = None


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
    # Nullable for configured-but-never-seen daemons (no runtime row yet).
    device_name: str | None = None
    providers: list[DaemonProviderOut]
    runtime_count: int
    last_seen: str | None = None
    online: bool
    # Phase A: static config overlay from ``~/.agt/daemons`` — the
    # reverse-dial URL and any declared capability labels. Absent for
    # daemons that only appear in the runtimes table (e.g. local daemons
    # that claim over HTTP long-poll, with no daemons-file entry).
    url: str | None = None
    capabilities: list[str] = []


class ListDaemonsResponse(BaseModel):
    """Response listing all known daemons."""

    daemons: list[DaemonOut]


class ResolveResponse(BaseModel):
    """Selection result for a dry-run resolve."""

    daemon_id: str
    provider: str
    identity_did: str


class GoalBudgetSummary(BaseModel):
    """Per-run spawn limits persisted on a goal root."""

    max_depth: int
    max_descendants: int
    max_batch: int


class GoalMetaSummary(BaseModel):
    """Plugin-facing subset of a goal root's ``goal_meta``."""

    goal: str
    template: str | None = None
    budget: GoalBudgetSummary | None = None
    goal_sha256: str | None = None


class GoalRunSummary(BaseModel):
    """One row in the goal-run history list."""

    root_job_id: str
    goal: str
    status: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    cwd: str | None = None
    created_at: str
    updated_at: str
    total_nodes: int
    status_counts: dict[str, int]
    meta: GoalMetaSummary


class GoalRunListResponse(BaseModel):
    runs: list[GoalRunSummary]
    total: int


class SopRunSummary(BaseModel):
    """One row in the sop-run history list."""

    root_job_id: str
    name: str
    status: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    cwd: str | None = None
    template_path: str | None = None
    template_hash: str | None = None
    created_at: str
    updated_at: str
    total_nodes: int
    status_counts: dict[str, int]


class SopRunListResponse(BaseModel):
    runs: list[SopRunSummary]
    total: int


class ChatRunSummary(BaseModel):
    """One row in the chat-run history list."""

    root_job_id: str
    session_id: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    status: str
    created_at: str
    updated_at: str


class ChatRunListResponse(BaseModel):
    runs: list[ChatRunSummary]
    total: int


class RunSummary(BaseModel):
    """Unified run row returned by /api/runs."""

    root_job_id: str
    mode: Literal["chat", "sop", "goal"]
    status: str
    provider: str
    identity_did: str | None = None
    daemon_id: str | None = None
    cwd: str | None = None
    template_path: str | None = None
    template_hash: str | None = None
    title: str | None = None
    created_at: str
    updated_at: str


class RunListResponse(BaseModel):
    runs: list[RunSummary]
    total: int


class UsageTotals(BaseModel):
    """Aggregated token/cost totals over a time window.

    ``steps`` counts only usage-bearing Steps (legacy / unreported rows are
    excluded), and ``turns`` is the summed per-Step ``num_turns``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    steps: int = 0
    turns: int = 0


class UsageSeriesPoint(BaseModel):
    """One time bucket of usage. Missing buckets are zero-filled server-side."""

    bucket: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    steps: int = 0


class UsageBreakdownItem(BaseModel):
    """Usage grouped by daemon × agent × model."""

    daemon_id: str | None = None
    agent: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cost_usd: float = 0.0
    steps: int = 0


class UsageSummaryResponse(BaseModel):
    """Payload for the web-console usage dashboard (``GET /api/usage/summary``)."""

    model_config = ConfigDict(populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    totals: UsageTotals
    series: list[UsageSeriesPoint]
    breakdown: list[UsageBreakdownItem]


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
    "ConsoleProjectCreate",
    "ConsoleProjectUpdate",
    "ConsoleProjectRunCounts",
    "ConsoleProjectOut",
    "ConsoleProjectListResponse",
    "ConsoleDirectoryOut",
    "ConsoleDirectoryListResponse",
    "ConsoleDirectoryPickResponse",
    "IdentityCreate",
    "IdentityOut",
    "IdentityUpdate",
    "IdentityBindingCreate",
    "IdentityBindingUpdate",
    "IdentityBindingOut",
    "IdentityBindingOverviewOut",
    "IdentityOverviewOut",
    "ListIdentitiesResponse",
    "ListIdentitiesOverviewResponse",
    "ListBindingsResponse",
    "ResolveRequest",
    "ResolveResponse",
    "DaemonProviderOut",
    "DaemonOut",
    "ListDaemonsResponse",
    "GoalBudgetSummary",
    "GoalMetaSummary",
    "GoalRunSummary",
    "GoalRunListResponse",
    "SopRunSummary",
    "SopRunListResponse",
    "ChatRunSummary",
    "ChatRunListResponse",
    "RunSummary",
    "RunListResponse",
    "UsageTotals",
    "UsageSeriesPoint",
    "UsageBreakdownItem",
    "UsageSummaryResponse",
]
