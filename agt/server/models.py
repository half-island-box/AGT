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
    provider: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="online")
    device_name: Mapped[str] = mapped_column(String(64), nullable=False)
    last_seen: Mapped[str] = mapped_column(String(32), nullable=False, default="")


class TaskModel(Base):
    """A task tracked by the server."""

    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    stdout: Mapped[str | None] = mapped_column(Text, nullable=True)
    stderr: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
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
    # Phase 4 identity: Optional identity DID that this task was executed with.
    # NULL for legacy tasks or when no --identity flag was provided.
    identity_did: Mapped[str | None] = mapped_column(
        String(256), nullable=True, index=True
    )
    created_at: Mapped[str] = mapped_column(String(32), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)

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
    """A pointer mapping task_id+provider → session_id (no conversation content)."""

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("task_id", "provider", name="uq_session_task_provider"),
    )

    task_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(128), nullable=False)
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
    "TaskModel",
    "SessionModel",
    "IdentityModel",
    "IdentityBindingModel",
]