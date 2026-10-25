"""SQLAlchemy ORM models for the agt-server."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    synonym,
)


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class RuntimeModel(Base):
    """A runtime registered by some daemon."""

    __tablename__ = "runtimes"
    __table_args__ = (UniqueConstraint("daemon_id", "provider", name="uq_runtime_daemon_provider"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    daemon_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Phase 4: human-readable daemon label (from ~/.agt/daemons name or device
    # name). NULL for legacy rows / daemons that don't send it.
    daemon_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="online")
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen: Mapped[str] = mapped_column(String(32), nullable=False, default="")


def _root_job_id_default(context) -> str:
    """Default ``root_job_id`` to the row's own ``job_id`` (a root's root
    is itself). Only fires when the field is omitted at construction."""
    return context.get_current_parameters()["job_id"]


class JobModel(Base):
    """A complete job/event tracked by the server.

    A Job is the lifecycle owner for a multi-step task. Each Job contains
    one or more Step rows (``StepModel``), each Step representing a single
    immutable subprocess execution.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    # Phase 3: parent-child lineage (A2A referenceTaskIds). NULL for root
    # jobs. Deletion cascades children (consistent with cancel semantics).
    parent_job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Phase 3: denormalized root id for O(1) subtree queries (cancel
    # cascade, per-root fan-out budget). Plain indexed string, no FK —
    # avoids FK ordering issues on backfill; lineage CASCADE rides on
    # parent_job_id only. Root jobs carry their own job_id here.
    #
    # Python-side default (a root's root is itself) so direct
    # ``JobModel(...)`` constructions that omit the field — test fixtures,
    # one-off seeds — still satisfy NOT NULL. ``create_job`` always sets it
    # explicitly (child jobs inherit the parent's root), so the default
    # only ever fires for root jobs.
    root_job_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, default=_root_job_id_default
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", index=True
    )
    # Mode discriminator: "chat", "sop", "goal", or plain "task".
    # Previously only "chat" / "task"; Phase 6 unifies the three first-class
    # modes so runs can be filtered by (cwd, job_type).
    job_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="task", index=True
    )
    # Denormalized from the first Step for convenient querying.
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    identity_did: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    target_daemon_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Phase 5 (SOP): per-node activation metadata JSON — raw ``input``
    # (with ``${node.output}`` placeholders), declared ``output`` name,
    # explicit provider/daemon overrides, identity alias. Needed because
    # blocked jobs have no Step/input_message until activation, yet the
    # activation runs in a later transaction. NULL on non-SOP jobs.
    sop_node: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 5 loops: which declared loop this job belongs to, which
    # iteration it represents (0 for non-loop jobs), and a duplicated JSON
    # blob with loop-level config so the loop controller can recover state
    # without a separate table.
    sop_loop_name: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    sop_iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=0, index=True)
    sop_loop_meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Goal mode: root-Job metadata JSON — {"mode":"goal","budget":
    # {max_depth,max_descendants,max_batch},"template":…,"goal":…,
    # "goal_sha256":…}. Only goal roots write this; children, SOP nodes
    # and plain tasks stay NULL (budgets then fall back to constants).
    goal_meta: Mapped[str | None] = mapped_column(Text, nullable=True)
    work_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    agent_cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Phase 6: the user's project directory used to isolate run records.
    # For local runs this is the client cwd; for remote daemon runs it is
    # the daemon-side execution directory (agent_cwd / --dir).
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    steps: Mapped[list["StepModel"]] = relationship(
        "StepModel",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="StepModel.created_at",
        # Async sessions can't lazily load a relationship (MissingGreenlet),
        # so load Steps eagerly whenever a Job is fetched. ``create_job``
        # also wires the first Step into this collection in-memory.
        lazy="selectin",
    )

    # Phase 5 (SOP): DATA-dependency edges, deliberately orthogonal to the
    # spawn lineage above. A row (job_id=X, parent_job_id=Y) means "X
    # waits for Y's terminal state". ``parents`` = upstream set (= DSL
    # depends_on), ``dependents`` = who waits on me (wake/cancel fanout).
    # viewonly: deletions ride on the DB-side FK CASCADE; async service
    # code queries ``JobParentModel`` directly via select().
    parents: Mapped[list["JobModel"]] = relationship(
        "JobModel",
        secondary="job_parents",
        primaryjoin="JobModel.job_id == JobParentModel.job_id",
        secondaryjoin="JobModel.job_id == JobParentModel.parent_job_id",
        viewonly=True,
        lazy="selectin",
    )
    dependents: Mapped[list["JobModel"]] = relationship(
        "JobModel",
        secondary="job_parents",
        primaryjoin="JobModel.job_id == JobParentModel.parent_job_id",
        secondaryjoin="JobModel.job_id == JobParentModel.job_id",
        viewonly=True,
        lazy="selectin",
    )


class StepModel(Base):
    """A single Step (immutable subprocess execution) tracked by the server."""

    __tablename__ = "tasks"

    # Logical name is ``step_id``; the physical column remains ``task_id``
    # for database/API compatibility. ``task_id`` is a mapped synonym for
    # legacy callers and daemon-facing wire contracts.
    step_id: Mapped[str] = mapped_column("task_id", String(64), primary_key=True)
    task_id = synonym("step_id")
    job_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True
    )
    # Phase 4: indexed — the routing load aggregate GROUP BYs this column.
    daemon_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Step 3.6: daemon-targeted routing. NULL = free task (any matching
    # daemon may claim); non-NULL = pinned to that daemon only.
    target_daemon_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Set True on a report where the agent refused to resume a prior
    # session. Exposed on the status response so the CLI can drop a stale
    # cwd-store entry (mirrors the local daemon's resume_rejected handling).
    resume_rejected: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Structured outcome reported by the daemon (or derived by the server
    # for legacy daemons). Values: done, needs_input, error, spawn_subjob.
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 3: sidecar JSON for structured outcomes. Carries
    # ``{"children": [...]}`` spawn-intent envelopes when
    # ``outcome == "spawn_subjob"``. Kept separate from the enum-style
    # ``outcome`` column for backward compatibility.
    outcome_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Phase 2 artifact foundation: this Step's structured input as an
    # A2A-shaped Message JSON (role + parts). Always normalized at
    # persistence time — a plain prompt-only submission is stored as a
    # single-TextPart Message. NULL only on rows written before Phase 2.
    input_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    parent_task_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
        nullable=True,
    )
    # Phase 4 workdir: AGT metadata directory for this task, derived from
    # the user's cwd at submit time. NULL for legacy tasks (daemon falls
    # back to work_dir_root) and for remote-daemon tasks (daemon uses its
    # own fallback).
    work_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # cwd injection (Phase 4 workdir supplement): the agent subprocess's
    # working directory on the daemon machine. Forwarded via submit_task
    # when the user passed --dir (or auto-detected on a local daemon).
    # NULL = daemon uses its own fallback (``work_dir_root / provider /
    # task_id`` legacy). Distinct from ``work_dir`` (AGT metadata dir).
    agent_cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Phase 4 identity: Optional identity DID that this task was executed with.
    # NULL for legacy tasks or when no --identity flag was provided.
    identity_did: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    job: Mapped[JobModel | None] = relationship("JobModel", back_populates="steps")
    children: Mapped[list[StepModel]] = relationship(
        "StepModel",
        back_populates="parent",
        remote_side="StepModel.parent_task_id",
    )
    parent: Mapped[StepModel | None] = relationship(
        "StepModel",
        back_populates="children",
        remote_side="StepModel.step_id",
    )


class SessionModel(Base):
    """A pointer mapping (job_id, identity_did, provider) → session_id.

    Phase 4: the PK gains ``identity_did`` so the same ``job_id`` under
    different identities (or with no identity at all) is treated as
    distinct conversations. ``identity_did`` is ``""`` for identity-less
    (legacy) sessions — SQLite treats NULL as distinct in UNIQUE
    constraints, so the empty string is the sentinel that keeps
    ``(job_id, "", provider)`` unique.
    """

    __tablename__ = "sessions"

    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
    )
    identity_did: Mapped[str] = mapped_column(
        String(256), primary_key=True, default=""
    )
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Phase 6: mirror of JobModel.cwd for filtering sessions by project dir.
    cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True, index=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class ArtifactModel(Base):
    """A declared deliverable produced by one Step (Phase 2).

    An artifact is *explicitly declared* output (a file the agent placed
    in ``.agt/outputs/``), never an automatic capture of stdout, chat
    replies, or repo mutations. The DB row is the registry of record:
    lineage (job/step/identity), typing (mime), declared semantics
    (summary/status_flag), content addressing (sha256), and availability
    state. Bytes live either inline (<4KB), on the server disk
    (``storage_path``), or remain on the producing daemon
    (``local_path`` + ``origin_daemon_id``) for lazy cross-daemon fetch.

    Status lifecycle: ``registered`` → ``ready`` → ``lost``. ``ready``
    means the bytes are believed available (inline, server disk, or
    producer-local); ``lost`` is set when a fetch finds the producer's
    local file gone.
    """

    __tablename__ = "artifacts"

    artifact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    step_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    producer_identity_id: Mapped[str | None] = mapped_column(
        String(256), nullable=True
    )
    name: Mapped[str] = mapped_column(String(512), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Declared pass/fail flag from the agent's manifest ("pass"/"fail"/...).
    # Carries no Job/Step semantics — step success is StepModel.status.
    status_flag: Mapped[str | None] = mapped_column(String(32), nullable=True)
    size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Server-side materialized storage (>=4KB fetched/uploaded blobs).
    storage_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Small content stored directly in the DB (<4KB).
    inline_bytes: Mapped[bytes | None] = mapped_column(
        LargeBinary, nullable=True
    )
    # INTERNAL: producer-daemon local path. Never exposed in public
    # payloads — the public reference is agt-artifact://<artifact_id>.
    local_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    origin_daemon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="registered", index=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class IdentityModel(Base):
    """An identity (role) that can be bound to multiple runtimes."""

    __tablename__ = "identities"

    did: Mapped[str] = mapped_column(String(256), primary_key=True)
    daemon_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    capabilities: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False, default="private")
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


class IdentityBindingModel(Base):
    """Binding between an identity and a runtime (daemon + provider)."""

    __tablename__ = "identity_bindings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_did: Mapped[str] = mapped_column(
        String(256),
        ForeignKey("identities.did", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    daemon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class JobParentModel(Base):
    """One SOP data-dependency edge (Phase 5).

    Row ``(job_id=X, parent_job_id=Y)`` means **X waits for Y**: Y is an
    upstream of X; X is a dependent of Y. This is the DAG edge — the
    spawn lineage (``JobModel.parent_job_id``) is a different, orthogonal
    axis and MUST NOT be conflated with it. Both FKs CASCADE so deleting
    either endpoint drops the edge.
    """

    __tablename__ = "job_parents"

    job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    parent_job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        primary_key=True,
        index=True,
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)


class SpawnBatchModel(Base):
    """One idempotent child-spawn request made by a running Goal Step.

    MCP creates children while the parent agent is still running, so the
    report path needs a durable way to know that a Step already spawned
    work. The stored response is also replayed when an agent retries the
    same tool call after a transport timeout.
    """

    __tablename__ = "spawn_batches"
    __table_args__ = (
        UniqueConstraint(
            "parent_step_id",
            "idempotency_key",
            name="uq_spawn_batch_step_key",
        ),
        UniqueConstraint(
            "parent_step_id",
            "request_digest",
            name="uq_spawn_batch_step_digest",
        ),
    )

    spawn_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    parent_step_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    root_job_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("jobs.job_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)


# Compatibility alias: the ORM was originally exposed as ``TaskModel`` before
# the Job/Step split made its actual Step semantics explicit.
TaskModel = StepModel


__all__ = [
    "Base",
    "RuntimeModel",
    "JobModel",
    "StepModel",
    "TaskModel",
    "SessionModel",
    "IdentityModel",
    "IdentityBindingModel",
    "ArtifactModel",
    "JobParentModel",
    "SpawnBatchModel",
]
