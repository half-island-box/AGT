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
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


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
    one or more Step rows (``TaskModel``), each Step representing a single
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
    # "chat" or "task". Chat jobs derive needs_input from a clean exit so
    # the conversation can continue via /input; task jobs derive done.
    job_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default="task", index=True
    )
    # Denormalized from the first Step for convenient querying.
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    identity_did: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    target_daemon_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    work_dir: Mapped[str | None] = mapped_column(String(512), nullable=True)
    agent_cwd: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

    steps: Mapped[list["TaskModel"]] = relationship(
        "TaskModel",
        back_populates="job",
        cascade="all, delete-orphan",
        order_by="TaskModel.created_at",
        # Async sessions can't lazily load a relationship (MissingGreenlet),
        # so load Steps eagerly whenever a Job is fetched. ``create_job``
        # also wires the first Step into this collection in-memory.
        lazy="selectin",
    )


class TaskModel(Base):
    """A single Step (immutable subprocess execution) tracked by the server."""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    daemon_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
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
    children: Mapped[list[TaskModel]] = relationship(
        "TaskModel",
        back_populates="parent",
        remote_side="TaskModel.parent_task_id",
    )
    parent: Mapped[TaskModel | None] = relationship(
        "TaskModel",
        back_populates="children",
        remote_side="TaskModel.task_id",
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
    # Carries no Job/Step semantics — step success is TaskModel.status.
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


__all__ = [
    "Base",
    "RuntimeModel",
    "JobModel",
    "TaskModel",
    "SessionModel",
    "IdentityModel",
    "IdentityBindingModel",
    "ArtifactModel",
]
