"""agt-server — FastAPI app + uvicorn entry point."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI

from .config import DaemonTarget, ServerConfig, ServerConfigError
from .console import router as console_router
from .daemon_link import DaemonLink
from .database import bind_get_db, make_engine, make_session_factory, session_scope
from .mcp_server import build_catalog_mcp, catalog_mcp_asgi
from .middleware.auth import bind_verify_task_token
from .migrate_schema import ensure_schema, migrate_identities_and_sessions
from .models import Base
from .pidfile import (
    DEFAULT_PID_FILE,
    PID_FILE,
    _default_pid_file,
    _read_pidfile,
    _signal_pid,
)
from .routes.daemon_config import router as daemon_config_router
from .routes.daemons import router as daemons_router
from .routes.heartbeat import router as heartbeat_router
from .routes.identities import router as identities_router
from .routes.jobs import router as jobs_router
from .routes.projects import router as projects_router
from .routes.artifacts import router as artifacts_router
from .routes.context import router as context_router
from .routes.sop import router as sop_router
from .routes.goal import router as goal_router
from .routes.runtimes import router as runtimes_router
from .routes.runs import router as runs_router
from .routes.sessions import router as sessions_router
from .routes.tasks import router as tasks_router
from .routes.uploads import router as uploads_router
from .services.input_service import sweep_expired_uploads
from .services.runtime_service import prune_stale_runtimes

log = logging.getLogger(__name__)


def _is_loopback_bind(host: str) -> bool:
    """Whether a server bind address can only receive local connections."""
    value = host.strip("[]")
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value == "localhost"


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Build the FastAPI app, configured against the given ServerConfig."""
    cfg = config or ServerConfig.from_env()
    if (
        cfg.console_enabled
        and cfg.host not in {"127.0.0.1", "localhost", "::1"}
        and not cfg.console_allow_remote
    ):
        log.warning(
            "Browser console disabled for non-loopback bind %s; "
            "set AGT_CONSOLE_ALLOW_REMOTE=1 only behind an authenticated proxy",
            cfg.host,
        )
        cfg.console_enabled = False
    engine = make_engine(cfg)
    session_factory = make_session_factory(engine)
    # Phase B: ``daemon_id → DaemonTarget`` lookup, built once here and
    # shared by the reverse-dial ``catalog.list`` handler and the MCP tool
    # (mounted below) so their catalog overlays (url/capabilities/name)
    # can never diverge.
    daemon_defs = {t.daemon_id: t for t in cfg.daemons}

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
            # Expired chat attachments: same one-shot sweep idea. A live
            # job's already-materialized inputs are unaffected (the daemon
            # copied them at claim time).
            await sweep_expired_uploads(db)
            await db.commit()
        log.info("agt-server ready on %s:%d (db=%s)", cfg.host, cfg.port, cfg.db_path)
        if cfg.console_enabled:
            log.info("AGT console: http://127.0.0.1:%d/", cfg.port)

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
                db_path=cfg.db_path,
                daemon_defs=daemon_defs,
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

        # Phase B: run the MCP session manager for the server's lifetime.
        # Starlette only runs the ROOT app's lifespan, not a mounted
        # sub-app's — ``/mcp`` is mounted, so we start its session manager
        # here ourselves (same pattern the SDK documents for FastAPI).
        async with app.state.mcp.session_manager.run():
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

    # Phase B: mount the AGT MCP endpoint on the existing FastAPI app at
    # ``/mcp`` (Streamable HTTP) — no new listen port. The session manager's
    # lifespan is run inside our own lifespan (see above) because a mounted
    # sub-app's lifespan is not auto-run by Starlette.
    app.state.mcp = build_catalog_mcp(session_factory, daemon_defs, cfg.secret)
    app.mount("/mcp", catalog_mcp_asgi(app.state.mcp))

    app.include_router(runtimes_router)
    app.include_router(daemon_config_router)
    app.include_router(daemons_router)
    app.include_router(tasks_router)
    app.include_router(uploads_router)
    app.include_router(jobs_router)
    app.include_router(artifacts_router)
    app.include_router(context_router)
    app.include_router(sop_router)
    app.include_router(goal_router)
    app.include_router(runs_router)
    app.include_router(sessions_router)
    app.include_router(heartbeat_router)
    app.include_router(identities_router)
    if cfg.console_enabled:
        app.include_router(projects_router)
        app.include_router(console_router)

    return app


class _UnconfiguredApp:
    """Placeholder ASGI app used when the server is imported without a secret.

    This keeps ``uvicorn agt.server.main:app`` from crashing at import time;
    instead it fails clearly when uvicorn actually tries to serve traffic.
    """

    def __init__(self, message: str) -> None:
        self._message = message

    async def __call__(self, scope, receive, send):
        raise RuntimeError(self._message)


# Module-level default app for `uvicorn agt.server.main:app`.
# If no secret is configured, import succeeds but serving fails with a
# clear message instead of a traceback at module load time.
try:
    app = create_app()
except ServerConfigError as exc:
    app = _UnconfiguredApp(str(exc))


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
