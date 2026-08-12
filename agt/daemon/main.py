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
from ..probe.scanner import AgentProbe
from ..runtime.models import Runtime
from .config import DaemonConfig
from .daemon import Daemon
from .local_server import LocalResourceServer

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
) -> None:
    """Periodically claim tasks from the server, up to max_concurrent at a time."""
    while not shutdown.is_set():
        # If we've filled our concurrency budget, don't claim more.
        if semaphore.locked():
            await asyncio.sleep(CLAIM_INTERVAL)
            continue
        try:
            tasks = await client.claim_tasks(timeout_seconds=30)
        except Exception as exc:  # network blip etc.
            log.warning("claim failed: %s", exc)
            await asyncio.sleep(CLAIM_INTERVAL)
            continue

        for task_payload in tasks:
            asyncio.create_task(
                _execute_one(client, daemon, semaphore, shutdown, task_payload)
            )
        if not tasks:
            await asyncio.sleep(CLAIM_INTERVAL)


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
    semaphore: asyncio.Semaphore,
    shutdown: asyncio.Event,
) -> None:
    """Send periodic heartbeats. Tracks consecutive failures for reconnection logic."""
    consecutive_failures = 0
    backoff = RECONNECT_BASE_SECONDS
    while not shutdown.is_set():
        await asyncio.sleep(HEARTBEAT_INTERVAL)
        if shutdown.is_set():
            return
        # Best-effort: pass an active-task hint based on semaphore state.
        active = max(0, semaphore._value if hasattr(semaphore, "_value") else 0)  # noqa: SLF001
        try:
            await client.heartbeat(active_task_count=active)
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

    async with ServerClient(
        base_url=config.server_url, daemon_id=config.daemon_id
    ) as client:
        try:
            await client.register_runtimes(list(daemon._runtimes.values()))  # noqa: SLF001
            log.info("Registered runtimes with Server")
        except Exception as exc:
            log.warning("Could not register runtimes (server offline?): %s", exc)

        try:
            main_task = asyncio.ensure_future(
                asyncio.gather(
                    _claim_loop(client, daemon, semaphore, shutdown),
                    _heartbeat_loop(client, semaphore, shutdown),
                )
            )
            await main_task
        except asyncio.CancelledError:
            log.info("Main loops cancelled; shutting down...")
        finally:
            log.info("Draining in-flight tasks...")
            try:
                removed = await client.deregister_runtimes()
                log.info("Cleaned up %d runtime registration(s)", removed)
            except Exception as exc:
                log.warning("Could not deregister runtimes: %s", exc)
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