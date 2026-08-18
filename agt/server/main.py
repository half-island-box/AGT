"""agt-server — FastAPI app + uvicorn entry point."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from .config import DaemonTarget, ServerConfig
from .daemon_link import DaemonLink
from .database import bind_get_db, make_engine, make_session_factory, session_scope
from .middleware.auth import bind_verify_task_token
from .migrate_sessions import migrate_sessions_json
from .models import Base
from .routes.heartbeat import router as heartbeat_router
from .routes.runtimes import router as runtimes_router
from .routes.sessions import router as sessions_router
from .routes.tasks import router as tasks_router

log = logging.getLogger(__name__)


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the FastAPI app, configured against the given ServerConfig."""
    cfg = config or ServerConfig.from_env()
    engine = make_engine(cfg)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # PRAGMA WAL once at startup; aiosqlite is per-connection, so applying
        # it here is informational. Real WAL behavior is enforced at the
        # filesystem level when SQLite first opens the database.
        async with session_scope(session_factory) as db:
            await migrate_sessions_json(db, cfg.sessions_json_path)
        # Schema check: ensure tables exist (Alembic may not have been run).
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        log.info("agt-server ready on %s:%d (db=%s)", cfg.host, cfg.port, cfg.db_path)

        # Reverse-dial daemon-link pool (Step 3.5): one task per
        # configured daemon target. Each task dials the daemon's WS
        # endpoint, holds the link, reconnects with backoff on drop,
        # and answers inbound ``tasks.claim`` frames. We start them as
        # background tasks; failure of one daemon link doesn't affect
        # others or the HTTP API.
        shutdown_event = asyncio.Event()
        daemon_link_tasks: list[asyncio.Task] = []
        for target in cfg.daemons:
            link = DaemonLink(
                daemon_id=target.daemon_id,
                url=target.url,
                secret=cfg.secret,
                session_factory=session_factory,
            )
            daemon_link_tasks.append(
                asyncio.create_task(link.run(shutdown_event), name=f"daemon-link:{target.daemon_id}")
            )
        if cfg.daemons:
            log.info(
                "Reverse-dial pool: %d daemon link(s) → %s",
                len(cfg.daemons),
                ", ".join(t.daemon_id for t in cfg.daemons),
            )

        try:
            yield
        finally:
            log.info("Shutting down daemon-link pool…")
            shutdown_event.set()
            for t in daemon_link_tasks:
                t.cancel()
            # Give the tasks a moment to acknowledge shutdown, but don't
            # block forever — the server should exit promptly.
            for t in daemon_link_tasks:
                try:
                    await asyncio.wait_for(t, timeout=2.0)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            await engine.dispose()

    app = FastAPI(
        title="agt-server",
        version="0.2.0",
        description="Local agent hub: Agent Registry, Task Queue, Session DB.",
        lifespan=lifespan,
    )

    # Inject the session factory into FastAPI state so the get_db dependency
    # can reach it without global state.
    app.state.session_factory = session_factory
    app.state.config = cfg
    bind_get_db(app, session_factory)
    bind_verify_task_token(app, cfg.secret)

    app.include_router(runtimes_router)
    app.include_router(tasks_router)
    app.include_router(sessions_router)
    app.include_router(heartbeat_router)

    return app


# Module-level default app for `uvicorn agt.server.main:app`.
app = create_app()


def main() -> int:
    """Console-script entry point for `agt server start`."""
    import argparse

    parser = argparse.ArgumentParser(prog="agt-server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    parser.add_argument("--db-path", default=None)
    parser.add_argument("--sessions-json", default=None)
    args = parser.parse_args()
    return cli_main(args)


def cli_main(args) -> int:
    """Callable entry for the CLI — accepts a pre-built namespace.

    The CLI's ``server start`` command builds an argparse-style namespace
    and hands it here, so both paths share the same server-startup logic.
    """
    cfg = ServerConfig.from_env()
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port
    if args.db_path:
        cfg.db_path = args.db_path
    if getattr(args, "sessions_json", None):
        cfg.sessions_json = args.sessions_json

    server_app = create_app(cfg)
    uvicorn.run(
        server_app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    return 0


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    sys.exit(main())


__all__ = ["app", "create_app", "main", "cli_main"]