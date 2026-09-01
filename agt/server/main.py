"""agt-server — FastAPI app + uvicorn entry point."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from .config import DaemonTarget, ServerConfig
from .daemon_link import DaemonLink
from .database import bind_get_db, make_engine, make_session_factory, session_scope
from .middleware.auth import bind_verify_task_token
from .migrate_schema import ensure_schema, migrate_identities_and_sessions
from .migrate_sessions import migrate_sessions_json
from .models import Base
from .routes.daemons import router as daemons_router
from .routes.heartbeat import router as heartbeat_router
from .routes.identities import router as identities_router
from .routes.jobs import router as jobs_router
from .routes.runtimes import router as runtimes_router
from .routes.sessions import router as sessions_router
from .routes.tasks import router as tasks_router
from .services.runtime_service import prune_stale_runtimes

log = logging.getLogger(__name__)


# Default pid file location. Computed lazily so ``HOME`` overrides
# (set in tests / by users with non-default homes) take effect.
DEFAULT_PID_FILE = Path.home() / ".agt" / "server.pid"
# Kept as an alias for backward-compatible imports.
PID_FILE = DEFAULT_PID_FILE


def _default_pid_file() -> Path:
    """Return the canonical pid file path, resolving ``HOME`` at call time."""
    return Path.home() / ".agt" / "server.pid"


def _read_pidfile(path: Path | None = None) -> int | None:
    """Return the live pid stored in ``path``, or None if stale/missing.

    If ``path`` is None, falls back to the canonical ``~/.agt/server.pid``,
    computed lazily (respects runtime ``HOME``).
    """
    target = path if path is not None else _default_pid_file()
    if not target.exists():
        return None
    try:
        pid = int(target.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None
    # Probe liveness: kill(pid, 0) succeeds iff the process exists.
    try:
        os.kill(pid, 0)
        return pid
    except ProcessLookupError:
        return None
    except PermissionError:
        # Different user owns the process; treat as running but don't signal.
        return pid


def _signal_pid(pid: int, sig: int = signal.SIGTERM) -> None:
    """Send ``sig`` to ``pid``. Raises on ESRCH (process already gone)."""
    os.kill(pid, sig)


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
        # Schema check: ensure tables exist (Alembic may not have been run),
        # then apply any column migrations for pre-existing DB files.
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(ensure_schema)
        async with engine.begin() as conn:
            await conn.run_sync(migrate_identities_and_sessions, Path(cfg.db_path).parent)
        # One-shot Phase 1 sessions.json migration. Runs AFTER the schema
        # migrations so the sessions table already carries the Phase 4
        # (task_id, identity_did, provider) shape the ORM model expects.
        async with session_scope(session_factory) as db:
            await migrate_sessions_json(db, cfg.sessions_json_path)
        # Startup sweep: drop runtime rows whose ``last_seen`` is older than
        # the liveness TTL. These are remnants from daemons that crashed
        # without deregistering, or whose machine / id changed — without
        # this they'd linger forever (until the next daemon touched its own
        # rows). A live daemon heartbeats every 30s, so its rows are fresh
        # and we never disturb a healthy daemon here.
        async with session_scope(session_factory) as db:
            removed = await prune_stale_runtimes(db)
            if removed:
                log.info(
                    "Startup sweep: removed %d stale runtime row(s)", removed,
                )
        log.info("agt-server ready on %s:%d (db=%s)", cfg.host, cfg.port, cfg.db_path)

        # Optional PID file (detached mode): write the live pid so that
        # ``agt server stop`` / ``status`` can find and signal us. Only
        # written when ``cfg.pid_file`` is set (foreground mode skips).
        pid_path: Path | None = None
        if cfg.pid_file:
            pid_path = Path(cfg.pid_file)
            pid_path.parent.mkdir(parents=True, exist_ok=True)
            pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            log.info("Server pid file: %s", pid_path)

        # Reverse-dial daemon-link pool (Step 3.5): one task per
        # configured daemon target. Each task dials the daemon's WS
        # endpoint, holds the link, reconnects with backoff on drop,
        # and answers inbound ``tasks.claim`` frames. We start them as
        # background tasks; failure of one daemon link doesn't affect
        # others or the HTTP API.
        shutdown_event = asyncio.Event()
        daemon_links: dict[str, DaemonLink] = {}
        daemon_link_tasks: list[asyncio.Task] = []
        for target in cfg.daemons:
            link = DaemonLink(
                daemon_id=target.daemon_id,
                url=target.url,
                secret=cfg.secret,
                session_factory=session_factory,
                name=target.name,
            )
            daemon_links[target.daemon_id] = link
            daemon_link_tasks.append(
                asyncio.create_task(link.run(shutdown_event), name=f"daemon-link:{target.daemon_id}")
            )
        app.state.daemon_links = daemon_links
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
            if pid_path is not None:
                # Only remove the pid file if it still names *this* process.
                # A second server may have since overwritten it (e.g. after a
                # restart race); deleting it unconditionally would clobber the
                # other instance's pid file and leave it un-stoppable.
                try:
                    if pid_path.read_text(encoding="utf-8").strip() == str(os.getpid()):
                        pid_path.unlink()
                except FileNotFoundError:
                    pass
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
    app.include_router(daemons_router)
    app.include_router(tasks_router)
    app.include_router(jobs_router)
    app.include_router(sessions_router)
    app.include_router(heartbeat_router)
    app.include_router(identities_router)

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
    parser.add_argument("--detach", action="store_true",
                        help="Detach into the background (default pid file + "
                             "log file under ~/.agt/). Ignored here when "
                             "called directly — the CLI handles forking.")
    parser.add_argument("--log-file", default=None,
                        help="Log file path (detached mode).")
    parser.add_argument("--pid-file", default=None,
                        help="PID file path (detached mode, default "
                             "~/.agt/server.pid).")
    args = parser.parse_args()
    return cli_main(args)


def cli_main(args) -> int:
    """Callable entry for the CLI — accepts a pre-built namespace.

    Both ``agt server start`` (foreground and ``--detach``) and the
    detached child (``python -m agt.server.main``) build a namespace
    and hand it here, so all paths share the same server-startup logic.
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

    pid_file = getattr(args, "pid_file", None) or None
    log_file = getattr(args, "log_file", None) or None
    # Pid file is only meaningful when we actually forked off (detached
    # mode). Foreground runs leave it None so the lifespan skips writing.
    if pid_file:
        cfg.pid_file = str(pid_file)

    server_app = create_app(cfg)

    # uvicorn routes its loggers (uvicorn / uvicorn.access / uvicorn.error)
    # through handlers declared in ``log_config``. Detached mode swaps the
    # default StreamHandlers for FileHandlers pointing at the chosen log
    # file so nothing escapes to the parent's tty.
    uvicorn_kwargs: dict = {}
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        uvicorn_kwargs["log_config"] = _file_log_config(str(log_path))

    uvicorn.run(
        server_app,
        host=cfg.host,
        port=cfg.port,
        log_level="info",
        ws_ping_interval=20,
        ws_ping_timeout=20,
        **uvicorn_kwargs,
    )
    return 0


def _file_log_config(log_path: str) -> dict:
    """Build a uvicorn ``log_config`` dict that writes both the access
    and default handlers to ``log_path`` instead of stdout/stderr.

    ``disable_existing_loggers=False`` keeps the app's own loggers
    (``agt.server.*``, etc.) flowing into our FileHandler.
    """
    import copy

    import uvicorn.config as uvicorn_config

    config = copy.deepcopy(uvicorn_config.LOGGING_CONFIG)
    for handler_name in ("default", "access"):
        handler = config["handlers"][handler_name]
        handler["class"] = "logging.FileHandler"
        handler["filename"] = log_path
        handler.pop("stream", None)
    return config


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    sys.exit(main())


__all__ = ["app", "create_app", "main", "cli_main", "PID_FILE", "_read_pidfile", "_signal_pid"]