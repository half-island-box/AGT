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
from .config import DaemonConfig, DaemonConfigError
from .daemon import Daemon
from .local_server import LocalResourceServer
from .mcp_proxy import ProxyMcpServer
from .pidfile import default_pid_file as _PID_FILE, read_daemon_pidfile
from .ws_claim import (
    ClaimStateMachine,
    claim_tasks_ws_first,
    report_task_progress_ws_first,
    report_task_ws_first,
)
from .ws_server import DaemonWsServer

log = logging.getLogger("agt.daemon")


PID_FILE = _PID_FILE
HEARTBEAT_INTERVAL = 30
CLAIM_INTERVAL = 1
MAX_HEARTBEAT_FAILURES = 3
RECONNECT_BASE_SECONDS = 1
RECONNECT_CAP_SECONDS = 60


def _runtime_snapshot(daemon: Daemon) -> list[dict]:
    return [
        {
            "name": rt.name,
            "type": rt.type,
            "version": rt.version,
            "status": rt.status,
            "device_name": rt.device_name,
        }
        for rt in daemon._runtimes.values()  # noqa: SLF001
    ]


def _read_pidfile() -> int | None:
    return read_daemon_pidfile(PID_FILE)


def _write_pidfile() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(f"{os.getpid()}\n", encoding="utf-8")


def _clear_pidfile() -> None:
    # Only remove the pid file if it still names *this* process — a restart
    # race can overwrite it, and an unconditional unlink would clobber the
    # other daemon's pid file, leaving it un-stoppable.
    try:
        if PID_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            PID_FILE.unlink()
    except FileNotFoundError:
        pass


def _ensure_local_daemons_entry(ws_port: int) -> bool:
    """Append a local-machine entry to ``~/.agt/daemons`` if none exists.
    Returns True if a local entry exists on return (pre-existing or just
    appended), False when the append was refused.
    """
    from .config import _host_of_url, _is_local_host, _port_of_url

    path = Path.home() / ".agt" / "daemons"
    lines: list[str] = []
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            log.warning("Could not read %s: %s", path, exc)
            return False
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        url = parts[1].strip()
        if _port_of_url(url) == str(ws_port) and _is_local_host(
            _host_of_url(url)
        ):
            return True  # local entry already present
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fp:
            fp.write(f"local ws://0.0.0.0:{ws_port}\n")
    except OSError as exc:
        log.warning(
            "Could not append local entry to %s (%s). Fix the file's "
            "ownership/permissions, otherwise `agt chat` cannot detect "
            "this machine's daemon as local.", path, exc,
        )
        return False
    log.info("Registered local daemon entry: local ws://0.0.0.0:%d", ws_port)
    return True


async def _sleep_or_wake(
    ws_server: DaemonWsServer | None,
    timeout: float,
) -> None:
    """Sleep for ``timeout`` seconds, waking immediately on notify.

    HTTP-only daemons (``ws_server`` is ``None``) fall back to plain sleep.
    Reverse-dial daemons wait on ``ws_server.claim_wakeup`` so inbound
    ``tasks.available`` frames interrupt idle polling.
    """
    if ws_server is None:
        await asyncio.sleep(timeout)
        return
    event = ws_server.claim_wakeup
    if event.is_set():
        # A notify arrived while we were busy; consume it and poll now.
        event.clear()
        return
    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    else:
        event.clear()

# daemon获取server任务的核心方法
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
      use it as the WS source. 
    """
    # ``runtime_ids`` is derived from the daemon's probed runtimes.
    # Refreshing each tick ensures a runtime added after startup (via
    # ``probe_agents(force_refresh=True)``) is picked up.
    rids: list[str] = list(daemon.runtime_providers())
    spawned: set[asyncio.Task] = set()  # held to avoid GC mid-flight
    idle_log = _IdleLinkLog(ws_server is not None)

    while not shutdown.is_set():
        if semaphore.locked():
            await _sleep_or_wake(ws_server, CLAIM_INTERVAL)
            continue
        rids = list(daemon.runtime_providers())

        ws_source = _resolve_ws_source(ws_server)
        # Reverse-dial mode without a link: HTTP fallback is futile, just wait.
        if ws_source is None and ws_server is not None:
            idle_log.tick_no_link()
            await _sleep_or_wake(ws_server, CLAIM_INTERVAL)
            continue
        idle_log.tick_with_link(ws_source is not None)

        try:
            tasks, cancelled_ids = await _do_claim(client, ws_source, state, rids)
        except Exception as exc:  # network blip etc.
            log.warning("claim failed: %s", exc)
            await _sleep_or_wake(ws_server, CLAIM_INTERVAL)
            continue

        # Phase 4: claim-channel cancel delivery (HTTP daemons primarily;
        # the WS path returns the same list). Never blocks claiming.
        try:
            await _handle_cancelled_tasks(client, cancelled_ids, ws_server)
        except Exception as exc:
            log.warning("cancel delivery failed: %s", exc)

        for task_payload in tasks:
            task = asyncio.create_task(
                _execute_one(
                    client, daemon, semaphore, shutdown, task_payload,
                    ws_server=ws_server,
                    claim_state=state,
                )
            )
            spawned.add(task)
            task.add_done_callback(spawned.discard)
        if not tasks:
            await _sleep_or_wake(ws_server, CLAIM_INTERVAL)


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
) -> tuple[list[dict], list[str]]:
    """One claim attempt — WS via the orchestrator, or HTTP fallback.

    Returns ``(tasks, cancelled_task_ids)``: the second element is the
    Phase 4 cancel channel for HTTP long-poll daemons (reverse-dial
    daemons additionally receive ``tasks.cancel`` pushes).
    """
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


async def _handle_cancelled_tasks(
    client: ServerClient,
    cancelled_ids: list[str],
    ws_server: "DaemonWsServer | None",
) -> None:
    """Deliver claim-channel cancellations to local executions.

    For each id: cancel the running asyncio task if present (the
    executor then kills the subprocess and reports cancelled through
    the normal chain). If the id is NOT running locally the step either
    finished unreported or never started here — report a bare
    ``cancelled`` anyway so the server's unreported-cancellation list
    drains (the report sets exit_code).
    """
    if not cancelled_ids:
        return
    from . import running_tasks

    ws_source = _resolve_ws_source(ws_server) if ws_server is not None else None
    for tid in cancelled_ids:
        was_running = running_tasks.cancel(tid)
        if not was_running:
            await _safe_report(
                client,
                task_id=tid,
                exit_code=-1,
                stdout="",
                stderr="cancelled before local execution",
                duration=0.0,
                status="cancelled",
                session_id=None,
                resume_rejected=False,
                ws_source=ws_source,
                claim_state=None,
                artifacts=[],
                outcome=None,
                outcome_payload=None,
            )


async def _execute_one(
    client: ServerClient,
    daemon: Daemon,
    semaphore: asyncio.Semaphore,
    shutdown: asyncio.Event,
    task_payload: dict,
    *,
    ws_server: DaemonWsServer | None = None,
    claim_state: ClaimStateMachine | None = None,
) -> None:
    async with semaphore:
        if shutdown.is_set():
            return
        task_id = task_payload["task_id"]
        # Phase 4: index this execution for cancel delivery.
        from . import running_tasks

        running_tasks.register(task_id)
        try:
            # Phase 4 workdir supplement: the claim payload carries the
            # submitter's ``agent_cwd`` — the directory the agent subprocess
            # should run in on THIS machine (and where IDENTITY.md / AGENTS.md
            # are injected). It's a daemon-side path passed through verbatim;
            # the daemon enforces the allowlist and creates it if missing
            # (see ``Daemon.run`` and the executor). ``None`` → legacy
            # ``work_dir_root / provider / task_id`` fallback.
            raw_agent_cwd = task_payload.get("agent_cwd")
            agent_cwd: Path | None = None
            agt_meta_dir: Path | None = None
            if raw_agent_cwd:
                agent_cwd = Path(raw_agent_cwd)
            # Resolve the report link: use the same WS source the claim
            # used (re-resolved at execute time in case the link rotated
            # between claim and report).
            ws_source = (
                _resolve_ws_source(ws_server) if ws_server is not None else None
            )

            # Phase 2: structured input rendering. Legacy prompt-only tasks
            # (``message`` absent) use the wire prompt verbatim — zero change.
            # A structured message is rendered here, resolving FileParts to
            # readable paths; an unresolvable reference fails the Step
            # loudly (reported as failed, never silently degraded).
            effective_prompt = task_payload["prompt"]
            raw_message = task_payload.get("message")
            if raw_message:
                from ..task.message_render import render_task_message

                async def _artifact_resolver(artifact_id: str):
                    cur_ws = (
                        _resolve_ws_source(ws_server) if ws_server is not None else None
                    )
                    if cur_ws is not None and cur_ws.supports_rpc_v1:
                        try:
                            return await cur_ws.call(
                                "artifacts.get",
                                {"artifact_id": artifact_id},
                                timeout_ms=8000,
                            )
                        except Exception as exc:
                            log.warning("artifacts.get over WS failed: %s", exc)
                            return None
                    try:
                        content = await client.get_artifact_content(artifact_id)
                        import base64

                        return {"content_b64": base64.b64encode(content).decode()}
                    except Exception as exc:
                        log.warning("artifact content fetch failed: %s", exc)
                        return None

                try:
                    effective_prompt = await render_task_message(
                        raw_message,
                        agent_cwd=agent_cwd or Path.cwd(),
                        resolver=_artifact_resolver,
                    )
                except Exception as exc:
                    log.exception("task %s input rendering failed", task_id)
                    await _safe_report(
                        client,
                        task_id=task_id,
                        exit_code=-1,
                        stdout="",
                        stderr=f"input rendering failed: {exc}",
                        duration=0.0,
                        status="failed",
                        session_id=None,
                        resume_rejected=False,
                        ws_source=ws_source,
                        claim_state=claim_state,
                        artifacts=[],
                    )
                    return

            # Phase 5 remote streaming: report incremental stdout prefixes to
            # the Server while the agent runs. Re-resolves the WS source on
            # every sample (the link may have rotated mid-execution); failures
            # are swallowed inside ``report_task_progress_ws_first`` and the
            # executor sampler, so progress can never break the terminal
            # report path.
            async def _progress_cb(stdout_prefix: str) -> None:
                # Phase 3: length-preserving mask — spawn marker JSON must
                # never leak through live SSE frames; removal would shrink
                # the cumulative prefix and trip the server's shrink guard.
                from ..task.spawn_intent import mask_spawn_lines

                stdout_prefix = mask_spawn_lines(stdout_prefix)
                cur_ws = (
                    _resolve_ws_source(ws_server) if ws_server is not None else None
                )
                if claim_state is not None:
                    await report_task_progress_ws_first(
                        wsrpc=cur_ws,
                        http_client=client,
                        state=claim_state,
                        task_id=task_id,
                        stdout_prefix=stdout_prefix,
                    )
                else:
                    try:
                        await client.report_progress(
                            task_id=task_id, stdout_prefix=stdout_prefix,
                        )
                    except Exception:
                        pass

            try:
                result = await daemon.run(
                    provider=task_payload["provider"],
                    prompt=effective_prompt,
                    task_id=task_id,
                    resume_session_id=task_payload.get("resume_session_id"),
                    agent_cwd=agent_cwd,
                    agt_meta_dir=agt_meta_dir,
                    identity_did=task_payload.get("identity_did"),
                    # Option D: server-rendered AGENTS.md body, sent in the
                    # claim payload. The daemon writes it directly to
                    # ``IDENTITY.md`` without fetching from the Server.
                    identity_md=task_payload.get("identity_md"),
                    outputs_subdir=task_payload.get("outputs_subdir"),
                    progress_cb=_progress_cb,
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
                    ws_source=ws_source,
                    claim_state=claim_state,
                    artifacts=[],
                    outcome=None,
                    outcome_payload=None,
                )
                return

            from ..task.artifact_scan import scanned_to_report_item

            artifacts = [
                scanned_to_report_item(a) for a in getattr(result, "artifacts", [])
            ]

            # Phase 2 transport (HTTP-fallback topology): with no reverse-dial
            # WS link there is no server→daemon lazy-fetch channel, so large
            # (non-inline) content must reach the server eagerly before the
            # report. Registration is idempotent on (step, name, sha256), so
            # the report's metadata entry will simply re-attach to the row
            # created here. A failed upload degrades to metadata-only — the
            # report itself must always go through.
            if artifacts and ws_source is None:
                import base64

                for item, scanned in zip(artifacts, result.artifacts):
                    if item.get("inline_b64") is None and scanned.local_path:
                        try:
                            content = Path(scanned.local_path).read_bytes()
                            await client.upload_artifact(
                                task_id=task_id,
                                artifact=item,
                                content_b64=base64.b64encode(content).decode(),
                            )
                        except Exception as exc:
                            log.warning(
                                "eager artifact upload failed for %s/%s: %s",
                                task_id,
                                item.get("name"),
                                exc,
                            )

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
                ws_source=ws_source,
                claim_state=claim_state,
                artifacts=artifacts,
                outcome=getattr(result, "outcome", None),
                outcome_payload=getattr(result, "outcome_payload", None),
            )

        finally:
            running_tasks.unregister(task_id)

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
    ws_source: WsRpcLink | None = None,
    claim_state: ClaimStateMachine | None = None,
    artifacts: list | None = None,
    outcome: str | None = None,
    outcome_payload: dict | None = None,
) -> None:
    """Best-effort result write. WS-first in reverse-dial mode; HTTP otherwise.

    ``ws_source``/``claim_state`` are both ``None`` for local-daemon
    mode (HTTP only). The WS-first path is gated on ``claim_state``
    being non-``None`` so the orchestrator knows to honor the Uncertain
    safety window.
    """
    try:
        if claim_state is not None:
            await report_task_ws_first(
                wsrpc=ws_source,
                http_client=client,
                state=claim_state,
                task_id=task_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                status=status,
                session_id=session_id,
                resume_rejected=resume_rejected,
                artifacts=artifacts,
                outcome=outcome,
                outcome_payload=outcome_payload,
            )
        else:
            await client.report_task(
                task_id=task_id,
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                duration=duration,
                status=status,
                session_id=session_id,
                resume_rejected=resume_rejected,
                artifacts=artifacts,
                outcome=outcome,
                outcome_payload=outcome_payload,
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
        runtimes = _runtime_snapshot(daemon)

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
    proxy_mcp: ProxyMcpServer | None = None
    if config.listen_mode:
        # Reverse-dial mode. We always need a state machine for the
        # Uncertain safety window on the claim path.
        claim_state = ClaimStateMachine()

        async def _push_snapshot_on_connect(link: WsRpcLink) -> None:
            """Push the current runtime snapshot to the Server immediately
            after the link is established, so its registry reflects what
            we can do right now — not 30s later when the first periodic
            heartbeat lands. Idempotent on the Server side (upsert by
            daemon_id), so the subsequent heartbeat just refreshes
            ``last_seen``."""
            try:
                await link.call(
                    "runtimes.register",
                    {"runtimes": _runtime_snapshot(daemon)},
                    timeout_ms=5000,
                )
            except Exception as exc:
                log.debug("on-connect runtimes.register failed: %s", exc)

        daemon_url = config.daemon_url or f"ws://0.0.0.0:{config.ws_listen_port}"
        ws_server = DaemonWsServer(
            port=config.ws_listen_port,
            secret=config.server_secret,
            daemon_id=config.daemon_id,
            url=daemon_url,
            on_connect=_push_snapshot_on_connect,
        )
        await ws_server.start()
        log.info(
            "Reverse-dial mode: listening on 0.0.0.0:%d/daemon",
            config.ws_listen_port,
        )
        # Phase B: start the proxy MCP server so remote agents can query the
        # live catalog by forwarding ``catalog.list`` over the reverse-dial
        # link. Local (non-listen) daemons never start this — their agents
        # reach the Server's ``/mcp`` directly.
        proxy_mcp = ProxyMcpServer(ws_server, port=config.mcp_port)
        await proxy_mcp.start()

    async with ServerClient(
        base_url=config.server_url,
        daemon_id=config.daemon_id,
        daemon_name=config.daemon_name,
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
            if proxy_mcp is not None:
                try:
                    await proxy_mcp.stop()
                except Exception as exc:
                    log.warning("Proxy MCP stop failed: %s", exc)
            await local_server.stop()
            _clear_pidfile()


def cmd_start(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    # Register this machine in ~/.agt/daemons BEFORE from_env — the
    # identity derivation reads the file to find this daemon's entry.
    _ensure_local_daemons_entry(
        int(os.environ.get("AGT_DAEMON_WS_PORT", str(DaemonConfig().ws_listen_port)))
    )
    try:
        config = DaemonConfig.from_env()
    except DaemonConfigError as exc:
        print(f"Failed to load daemon configuration: {exc}", file=sys.stderr)
        print(
            "Fix or remove ~/.agt/daemon.json and restart the daemon.",
            file=sys.stderr,
        )
        return 2
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
