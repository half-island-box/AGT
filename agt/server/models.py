"""SQLAlchemy ORM models for the agt-server."""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Integer,
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


class JobModel(Base):
    """A complete job/event tracked by the server.

    A Job is the lifecycle owner for a multi-step task. Each Job contains
    one or more Step rows (``TaskModel``), each Step representing a single
    immutable subprocess execution.
    """

    __tablename__ = "jobs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
]