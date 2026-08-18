"""Daemon main entry — turns the in-process Daemon into a long-lived
background process that talks to the Server.

Usage:
    agt daemon start
    agt daemon stop    (signals an already-running daemon to exit)
    agt daemon status  (prints whether the pidfile is alive)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys
from pathlib import Path

from ..comm import ServerClient
from ..comm.wsrpc_link import WsRpcLink
from ..probe.scanner import AgentProbe
from ..runtime.models import Runtime
from ..server.services.auth_service import TaskToken
from .config import DaemonConfig
from .daemon import Daemon
from .local_server import LocalResourceServer
from .ws_claim import ClaimStateMachine, claim_tasks_ws_first
from .ws_server import DaemonWsServer

log = logging.getLogger("agt.daemon")


PID_FILE = Path.home() / ".agt" / "daemon.pid"
HEARTBEAT_INTERVAL = 30
CLAIM_INTERVAL = 1
MAX_HEARTBEAT_FAILURES = 3
RECONNECT_BASE_SECONDS = 1
RECONNECT_CAP_SECONDS = 60


def _pidfile_alive(pid: int) -> bool:
    """Return True if a process with this pid is alive AND belongs to a daemon."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # Best-effort: check /proc for the command line; tolerate failure on non-Linux.
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
        return "agt" in cmdline and "daemon" in cmdline
    except OSError:
        return True  # can't verify, assume alive


def _read_pidfile() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid if _pidfile_alive(pid) else None


def _write_pidfile() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _clear_pidfile() -> None:
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


async def _claim_loop(
    client: ServerClient,
    daemon: Daemon,
    semaphore: asyncio.Semaphore,
    shutdown: asyncio.Event,
    *,
    state: ClaimStateMachine | None = None,
    ws_server: DaemonWsServer | None = None,
) -> None:
    """Periodically claim tasks from the server, up to max_concurrent at a time.

    Two modes:

    - **HTTP-only** (``ws_server`` is ``None``): local daemon, plain
      ``client.claim_tasks`` over HTTP.
    - **Reverse dial** (``ws_server`` provided): daemon listens, server
      dials in (Step 3.5). When ``ws_server.current_link`` is set, we
      use it as the WS source. When the link is missing, the HTTP
      fallback is futile (server is in NAT — that's why we went
      reverse), so we log once and sleep silently until the link
      comes up.
    """
    # ``runtime_ids`` is derived from the daemon's probed runtimes.
    # Refreshing each tick ensures a runtime added after startup (via
    # ``probe_agents(force_refresh=True)``) is picked up.
    rids: list[str] = list(daemon.runtime_providers())
    spawned: set[asyncio.Task] = set()  # held to avoid GC mid-flight
    idle_log = _IdleLinkLog(ws_server is not None)

    while not shutdown.is_set():
        if semaphore.locked():
            await asyncio.sleep(CLAIM_INTERVAL)
            continue
        rids = list(daemon.runtime_providers())

        ws_source = _resolve_ws_source(ws_server)
        # Reverse-dial mode without a link: HTTP fallback is futile, just wait.
        if ws_source is None and ws_server is not None:
            idle_log.tick_no_link()
            await asyncio.sleep(CLAIM_INTERVAL)
            continue
        idle_log.tick_with_link(ws_source is not None)

        try:
            tasks = await _do_claim(client, ws_source, state, rids)
        except Exception as exc:  # network blip etc.
            log.warning("claim failed: %s", exc)
            await asyncio.sleep(CLAIM_INTERVAL)
            continue

        for task_payload in tasks:
            task = asyncio.create_task(
                _execute_one(client, daemon, semaphore, shutdown, task_payload)
            )
            spawned.add(task)
            task.add_done_callback(spawned.discard)
        if not tasks:
            await asyncio.sleep(CLAIM_INTERVAL)


class _IdleLinkLog:
    """Track the link state so we log the idle/active transition only once.

    Without this, reverse-dial mode logs every tick ("waiting for link…")
    which spams the log when the server isn't dialed in yet.
    """

    def __init__(self, has_ws_server: bool) -> None:
        self._relevant = has_ws_server
        # True when the *previous* tick had no link; we emit the
        # "waiting" log on the first such tick and then go silent.
        self._was_idle = False

    def tick_no_link(self) -> None:
        if not self._relevant or self._was_idle:
            return
        log.info(
            "claim loop: no WS link yet (server hasn't dialed) — waiting"
        )
        self._was_idle = True

    def tick_with_link(self, has_link: bool) -> None:
        # ``has_link`` is True if a WS source is available this tick.
        if not self._relevant or not self._was_idle or not has_link:
            return
        log.info("claim loop: WS link available, starting claim cycle")
        self._was_idle = False


def _resolve_ws_source(
    ws_server: DaemonWsServer | None,
) -> WsRpcLink | None:
    """Return the server-dialed link if available, else ``None``."""
    if ws_server is not None:
        link = ws_server.current_link
        if link is not None and not link.closed:
            return link
    return None


async def _do_claim(
    client: ServerClient,
    ws_source: WsRpcLink | None,
    state: ClaimStateMachine | None,
    rids: list[str],
) -> list[dict]:
    """Run one claim attempt — WS via the orchestrator, or HTTP fallback."""
    if ws_source is not None and state is not None:
        return await claim_tasks_ws_first(
            wsrpc=ws_source,
            http_client=client,
            state=state,
            runtime_ids=rids,
            max_tasks=1,
        )
    return await client.claim_tasks(
        timeout_seconds=30,
        runtime_ids=rids,
    )


async def _execute_one(
    client: ServerClient,
    daemon: Daemon,
    semaphore: asyncio.Semaphore,
    shutdown: asyncio.Event,
    task_payload: dict,
) -> None:
    async with semaphore:
        if shutdown.is_set():
            return
        task_id = task_payload["task_id"]
        try:
            result = await daemon.run(
                provider=task_payload["provider"],
                prompt=task_payload["prompt"],
                task_id=task_id,
                resume_session_id=task_payload.get("resume_session_id"),
            )
        except Exception as exc:
            log.exception("task %s execution failed", task_id)
            await _safe_report(
                client,
                task_id=task_id,
                exit_code=-1,
                stdout="",
                stderr=str(exc),
                duration=0.0,
                status="failed",
                session_id=None,
                resume_rejected=False,
            )
            return

        await _safe_report(
            client,
            task_id=task_id,
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration=result.duration,
            status=result.status,
            session_id=result.session_id,
            resume_rejected=result.resume_rejected,
        )


async def _safe_report(
    client: ServerClient,
    *,
    task_id: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    duration: float,
    status: str,
    session_id: str | None,
    resume_rejected: bool,
) -> None:
    try:
        await client.report_task(
            task_id=task_id,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            status=status,
            session_id=session_id,
            resume_rejected=resume_rejected,
        )
    except Exception as exc:
        log.warning("report failed for task %s: %s", task_id, exc)


async def _heartbeat_loop(
    client: ServerClient,
    daemon: Daemon,
    semaphore: asyncio.Semaphore,
    shutdown: asyncio.Event,
    *,
    ws_server: DaemonWsServer | None = None,
) -> None:
    """Send periodic heartbeats / runtime snapshots.

    Two paths:

    - **Local HTTP daemon** (``ws_server=None``): send an HTTP
      ``POST /api/heartbeat`` with the runtime snapshot + active-task
      hint, with backoff on consecutive failures.
    - **Reverse-dial daemon** (``ws_server`` provided): push the same
      snapshot over the server-dialed link via the ``runtimes.register``
      RPC. The HTTP direction is futile (server is in NAT), and no
      link means "server hasn't dialed yet" — we just wait silently
      rather than spamming.

    Both keep the server's ``runtimes`` table in sync with whatever the
    daemon's probe currently sees, so a newly installed / uninstalled /
    upgraded agent CLI shows up without a daemon restart.
    """
    consecutive_failures = 0
    backoff = RECONNECT_BASE_SECONDS
    while not shutdown.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if shutdown.is_set():
            return
        # Snapshot of currently-probed runtimes (same shape in both paths).
        runtimes = [
            {
                "name": rt.name,
                "type": rt.type,
                "version": rt.version,
                "status": rt.status,
                "device_name": rt.device_name,
            }
            for rt in daemon._runtimes.values()  # noqa: SLF001
        ]

        # Reverse-dial: sync over the WS link.
        if ws_server is not None:
            link = ws_server.current_link
            if link is None or link.closed:
                # No link — server hasn't dialed yet. Wait silently.
                continue
            try:
                await link.call(
                    "runtimes.register",
                    {"runtimes": runtimes},
                    timeout_ms=5000,
                )
                consecutive_failures = 0
                backoff = RECONNECT_BASE_SECONDS
            except Exception as exc:
                consecutive_failures += 1
                log.debug("runtimes.register over WS failed: %s", exc)
            continue

        # Local daemon: HTTP heartbeat.
        active = max(0, semaphore._value if hasattr(semaphore, "_value") else 0)  # noqa: SLF001
        try:
            await client.heartbeat(active_task_count=active, runtimes=runtimes)
            consecutive_failures = 0
            backoff = RECONNECT_BASE_SECONDS
        except Exception as exc:
            consecutive_failures += 1
            log.warning(
                "heartbeat failed (%d/%d): %s",
                consecutive_failures,
                MAX_HEARTBEAT_FAILURES,
                exc,
            )
            if consecutive_failures >= MAX_HEARTBEAT_FAILURES:
                log.warning(
                    "Server unreachable for %d consecutive heartbeats; backing off %ds",
                    consecutive_failures,
                    backoff,
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, RECONNECT_CAP_SECONDS)


async def run_background(config: DaemonConfig) -> None:
    """The main coroutine for a long-running daemon process."""
    if _read_pidfile() is not None:
        log.error("Daemon already running (pidfile %s present). Exiting.", PID_FILE)
        raise SystemExit(1)
    _write_pidfile()

    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    main_task: asyncio.Task[None] | None = None

    def _signal_handler() -> None:
        log.info("Shutdown signal received; draining...")
        shutdown.set()
        if main_task is not None:
            main_task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, _signal_handler)

    # Start local resource server so spawned agent subprocesses can call us.
    local_server = LocalResourceServer(
        port=config.daemon_port,
        local_token=config.local_token,
    )
    await local_server.start()
    # Make the port + token visible to subprocesses we spawn.
    os.environ["AGT_DAEMON_PORT"] = str(config.daemon_port)
    os.environ["AGT_DAEMON_TOKEN"] = config.local_token

    # Build the in-process Daemon (Phase 1 code path) — TaskExecutor + SessionStore.
    daemon = Daemon(config=config, probe=AgentProbe())

    # Prime the in-process Daemon's runtime map so we can register with the Server.
    await daemon.start()

    semaphore = asyncio.Semaphore(config.max_concurrent_tasks)

    # Reverse-dial mode (Step 3.5): daemon listens, server dials in.
    # When disabled, the daemon claims via plain HTTP (local-daemon path).
    claim_state: ClaimStateMachine | None = None
    ws_server: DaemonWsServer | None = None
    if config.listen_mode:
        # Reverse-dial mode. We always need a state machine for the
        # Uncertain safety window on the claim path.
        claim_state = ClaimStateMachine()
        ws_server = DaemonWsServer(
            port=config.ws_listen_port,
            secret=config.server_secret,
        )
        await ws_server.start()
        log.info(
            "Reverse-dial mode: listening on 0.0.0.0:%d/daemon",
            config.ws_listen_port,
        )

    async with ServerClient(
        base_url=config.server_url, daemon_id=config.daemon_id
    ) as client:
        # In reverse-dial mode the daemon→server HTTP direction is
        # generally futile (the server is in NAT). Demote register /
        # heartbeat failures to debug so the log isn't spammed by
        # expected errors.
        http_log = log.debug if ws_server is not None else log.warning
        try:
            await client.register_runtimes(list(daemon._runtimes.values()))  # noqa: SLF001
            log.info("Registered runtimes with Server")
        except Exception as exc:
            http_log(
                "Could not register runtimes (server unreachable?): %s", exc
            )

        try:
            main_task = asyncio.ensure_future(
                asyncio.gather(
                    _claim_loop(
                        client, daemon, semaphore, shutdown,
                        state=claim_state,
                        ws_server=ws_server,
                    ),
                    _heartbeat_loop(
                        client, daemon, semaphore, shutdown,
                        ws_server=ws_server,
                    ),
                )
            )
            await main_task
        except asyncio.CancelledError:
            log.info("Main loops cancelled; shutting down...")
            raise
        finally:
            log.info("Draining in-flight tasks...")
            try:
                removed = await client.deregister_runtimes()
                log.info("Cleaned up %d runtime registration(s)", removed)
            except Exception as exc:
                log.warning("Could not deregister runtimes: %s", exc)
            if ws_server is not None:
                try:
                    await ws_server.stop()
                except Exception as exc:
                    log.warning("WS server stop failed: %s", exc)
            await local_server.stop()
            _clear_pidfile()


def cmd_start(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    config = DaemonConfig.from_env()
    if args.server_url:
        config.server_url = args.server_url
    try:
        asyncio.run(run_background(config))
    except KeyboardInterrupt:
        pass
    return 0


def cmd_stop(_args: argparse.Namespace) -> int:
    pid = _read_pidfile()
    if pid is None:
        print("No daemon running (pidfile absent or stale).")
        return 0
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        print(f"Failed to signal pid {pid}: {exc}")
        return 1
    print(f"Sent SIGTERM to daemon pid {pid}")
    return 0


def cmd_status(_args: argparse.Namespace) -> int:
    pid = _read_pidfile()
    if pid is None:
        print("Daemon is NOT running.")
        return 1
    print(f"Daemon is running, pid={pid}, pidfile={PID_FILE}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="agt-daemon")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Start the background daemon")
    p_start.add_argument("--server-url", default=None, help="Override AGT_SERVER_URL")
    p_start.set_defaults(func=cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the running daemon")
    p_stop.set_defaults(func=cmd_stop)

    p_status = sub.add_parser("status", help="Show daemon status")
    p_status.set_defaults(func=cmd_status)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["run_background", "PID_FILE", "main"]