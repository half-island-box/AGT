"""Database engine and session factory for the agt-server."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import ServerConfig

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker as _ASM


def make_engine(config: ServerConfig) -> AsyncEngine:
    """Build an AsyncEngine bound to the configured SQLite file."""
    db_path = Path(config.db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    engine = create_async_engine(url, echo=False, future=True)
    return engine


def make_session_factory(engine: AsyncEngine) -> "async_sessionmaker[AsyncSession]":
    return async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


@asynccontextmanager
async def session_scope(factory) -> AsyncIterator[AsyncSession]:
    """Context manager that yields a session and commits/rolls back."""
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()


async def get_db_factory():
    """Placeholder used by ``app.dependency_overrides[get_db]``."""
    raise RuntimeError("get_db must be overridden in app factory")


async def get_db():
    """FastAPI dependency — overridden in ``create_app`` to bind a session factory."""
    raise RuntimeError("get_db dependency not configured")


def bind_get_db(app, factory) -> None:
    """Install the closure-based dependency override on the FastAPI app."""
    async def _override():
        async with session_scope(factory) as session:
            yield session
    app.dependency_overrides[get_db] = _override


def get_config(request: Request) -> ServerConfig:
    """FastAPI dependency that returns the ``ServerConfig`` stored on app.state.

    ``ServerConfig`` is set in ``create_app`` via ``app.state.config = cfg``.
    """
    cfg = getattr(request.app.state, "config", None)
    if cfg is None:
        raise RuntimeError("ServerConfig not bound to app.state")
    return cfg


__all__ = [
    "make_engine",
    "make_session_factory",
    "session_scope",
    "get_db",
    "bind_get_db",
    "get_config",
]