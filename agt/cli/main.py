"""``agt`` CLI entry point — Click-based command surface.

Phase 2 adds:
- ``agt server start`` — start the FastAPI Server in the foreground.
- ``agt task submit`` — submit a task to the Server (returns task_id).
- ``agt task status`` — query task status from the Server.
- ``agt daemon start|stop|status`` — daemon lifecycle (replaces Phase 1 placeholder).

Phase 1 commands (``probe`` / ``run`` / ``chat``) still run **locally**
without the Server, preserving the no-daemon-required workflow.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
import uuid
from pathlib import Path

import click

from ..comm import ServerClient
from ..daemon.daemon import Daemon as DaemonController
from ..runtime.models import AgentEntry
from ..server.services.auth_service import TaskToken


# ── workdir helpers (Phase 4) ──────────────────────────────────────


class CLIError(RuntimeError):
    """Raised by CLI helpers to surface user-visible errors with sys.exit."""


# System-directory blacklist for ``_validate_cwd``. A cwd inside one of these
# is a misuse — AGT metadata and agent cwd shouldn't pollute system paths.
_SYSTEM_DIR_BLACKLIST: tuple[Path, ...] = (
    Path("/"),
    Path("/usr"),
    Path("/etc"),
    Path("/var"),
    Path.home() / ".local" / "share" / "Trash",
)


def _resolve_agt_meta_dir(
    cwd: Path,
    task_id: str,
    explicit: Path | None,
    *,
    legacy_root: Path | None = None,
) -> Path:
    """Compute the AGT metadata directory for a task.

    Priority:
      1. ``explicit`` (from ``--work-dir``) — used verbatim (resolved to
         absolute). Agent cwd is NOT changed.
      2. ``<cwd>/.agt/tasks/<task_id>/`` — the standard cwd-derived path.
      3. ``legacy_root / provider / task_id`` — only when ``legacy_root``
         is given (i.e. ``--no-work-dir`` was passed and the caller
         supplies the legacy daemon-root). Without ``legacy_root`` we
         still return the cwd-derived path; callers that want legacy
         behavior handle it themselves.
    """
    if explicit is not None:
        return explicit.resolve()
    if legacy_root is not None:
        # legacy_root takes precedence over cwd-derived when explicitly
        # requested. Used by ``--no-work-dir`` which falls back to the
        # old ``work_dir_root / provider / task_id`` layout (the caller
        # passes the resolved path).
        return legacy_root
    return (cwd / ".agt" / "tasks" / task_id).resolve()


def _validate_cwd(cwd: Path) -> None:
    """Validate the user's cwd as a usable workdir parent.

    Raises ``CLIError`` on:
      - cwd not writable
      - cwd on system-directory blacklist
      - ``<cwd>/.agt`` exists but is not a directory
    """
    if not os.access(cwd, os.W_OK):
        raise CLIError(f"cwd '{cwd}' is not writable")
    resolved = cwd.resolve()
    for blocked in _SYSTEM_DIR_BLACKLIST:
        if blocked.resolve() == resolved:
            raise CLIError(
                f"cwd '{cwd}' is a system path; refusing to use as workdir parent"
            )
    agt_dir = cwd / ".agt"
    if agt_dir.exists() and not agt_dir.is_dir():
        raise CLIError(f"'{agt_dir}' exists but is not a directory")


def _ensure_gitignore(cwd: Path) -> None:
    """Idempotently append ``.agt/`` to ``<cwd>/.gitignore`` when cwd is a
    git project and the entry isn't already listed.

    Never calls ``git add`` or ``git commit``. Never strips existing
    entries. Idempotent under repeated invocation.
    """
    if not (cwd / ".git").exists():
        return
    gitignore = cwd / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    # Match any per-line gitignore entry that already covers ``.agt/``:
    # the literal entry, the bare token, or a wildcard like ``.agt*`` /
    # ``.agt/**`` that subsumes it.
    if any(
        line.strip() in (".agt", ".agt/", ".agt*", ".agt/**")
        or line.strip().startswith((".agt/", ".agt*", ".agt/**"))
        for line in existing.splitlines()
    ):
        return
    with gitignore.open("a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write("\n# AGT metadata (added by `agt run`)\n")
        f.write(".agt/\n")


@click.group()
@click.version_option()
def cli() -> None:
    """agt — discover and invoke local AI agent CLIs."""


# ── probe ──────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--refresh", is_flag=True, help="Force re-scan, ignoring cached results."
)
def probe(refresh: bool) -> None:
    """Discover AI agent CLIs installed on this machine."""
    controller = DaemonController()

    async def _run() -> list:
        return await controller.probe_agents(force_refresh=refresh)

    entries = asyncio.run(_run())

    if not entries:
        click.echo("No known AI agent CLIs found on this machine.", err=True)
        sys.exit(1)

    click.echo("🔍 Scanning for AI agent CLIs on this machine...\n")
    click.echo(
        f"  {'Provider':<10} {'Command':<14} {'Path':<40} {'Version':<12} {'Status'}"
    )
    click.echo("  " + "─" * 90)
    for entry in entries:
        click.echo(
            f"  {entry.provider:<10} {entry.command:<14} "
            f"{str(entry.path):<40} {entry.version:<12} "
            f"{'online' if entry.is_available() else 'offline'}"
        )
    click.echo(f"\n✅ Found {len(entries)} agent(s) on this machine.")


def _echo_task_result(data: dict) -> None:
    """Print a task's final status + output (shared by ``run --daemon`` and
    ``task status``)."""
    stdout = data.get("stdout") or ""
    stderr = data.get("stderr") or ""
    duration = data.get("duration")
    duration_str = f"{duration:.2f}s" if duration is not None else "—"
    click.echo(
        f"task_id   : {data.get('task_id')}\n"
        f"provider  : {data.get('provider')}\n"
        f"status    : {data.get('status')}\n"
        f"session_id: {data.get('session_id') or '—'}\n"
        f"exit_code : {data.get('exit_code')}\n"
        f"duration  : {duration_str}"
    )
    if stdout:
        click.echo(f"\n--- stdout ---\n{stdout}", nl=False)
        if not stdout.endswith("\n"):
            click.echo()
        click.echo("--- end stdout ---")
    if stderr:
        click.echo(f"\n--- stderr ---\n{stderr}", nl=False)
        if not stderr.endswith("\n"):
            click.echo()
        click.echo("--- end stderr ---")


def _prepare_workdir(
    cwd: Path,
    task_id: str,
    work_dir_arg: str | None,
    no_work_dir: bool,
) -> tuple[Path, str | None]:
    """Validate cwd, ensure gitignore, resolve meta dir.

    Returns ``(agt_meta_dir, work_dir_str)`` where ``work_dir_str`` is the
    string sent to the Server (``None`` when ``--no-work-dir`` was given,
    so the daemon uses its legacy fallback path).
    """
    if not no_work_dir:
        try:
            _validate_cwd(cwd)
        except CLIError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        _ensure_gitignore(cwd)
    explicit = Path(work_dir_arg).resolve() if work_dir_arg else None
    legacy_root = cwd if no_work_dir else None
    agt_meta_dir = _resolve_agt_meta_dir(
        cwd, task_id, explicit, legacy_root=legacy_root,
    )
    work_dir_str = None if no_work_dir else str(agt_meta_dir)
    return agt_meta_dir, work_dir_str


async def _submit_and_wait(
    server_url: str,
    *,
    prompt: str,
    provider: str,
    task_id: str,
    target_daemon_id: str,
    work_dir_str: str | None,
) -> dict | None:
    """Submit a task to the Server and poll until terminal. Returns the
    final task dict, or ``None`` if the daemon never responded."""
    async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
        assigned = await client.submit_task(
            prompt=prompt,
            provider=provider,
            task_id=task_id,
            target_daemon_id=target_daemon_id,
            work_dir=work_dir_str,
        )
        try:
            return await client.wait_task(assigned)
        except TimeoutError:
            click.echo(
                f"⚠️  Task {assigned} still pending — is daemon "
                f"'{target_daemon_id}' online?",
                err=True,
            )
            return None


def _run_local(
    prompt: str,
    provider: str,
    agent_cwd: Path,
    agt_meta_dir: Path,
) -> None:
    """Run the agent on this machine (no Server round-trip). Exits 0/2."""
    controller = DaemonController()
    click.echo(f"🚀 Executing with {provider}...\n")

    async def _run():
        return await controller.run(
            provider=provider,
            prompt=prompt,
            agent_cwd=agent_cwd,
            agt_meta_dir=agt_meta_dir,
        )

    result = asyncio.run(_run())

    emoji = "✅" if result.status == "completed" else "❌"
    click.echo(
        f"\n{emoji} Task {result.status} in {result.duration:.1f}s "
        f"(exit_code={result.exit_code}, session_id={result.session_id or '—'})"
    )
    if result.resume_rejected:
        click.echo(
            "⚠️  Resume was rejected; session was cleared. Next run will be fresh."
        )
    if result.status != "completed" and result.stderr:
        # stderr is silenced on the live stream; surface it only when the
        # agent actually failed, so the user sees *why*.
        click.echo(f"\n--- stderr ---\n{result.stderr}", nl=False)
        if not result.stderr.endswith("\n"):
            click.echo()
        click.echo("--- end stderr ---")
    sys.exit(0 if result.status == "completed" else 2)


def _local_candidate_providers(
    controller: DaemonController | None = None,
) -> list[str]:
    """Run the local probe and return provider names whose binary is available.

    When ``controller`` is given, reuse it so the probe cache (held on
    ``AgentProbe``) is shared across calls — important for ``chat``, where
    we resolve the provider once but reuse the same ``DaemonController``
    for every turn. A fresh ``DaemonController`` would re-shell out to
    each agent's ``--version`` on every turn.
    """
    async def _run(c: DaemonController) -> list[AgentEntry]:
        return await c.probe_agents()

    c = controller or DaemonController()
    entries = asyncio.run(_run(c))
    return [e.provider for e in entries if e.is_available()]


def _remote_candidate_providers(
    target_daemon_id: str, server_url: str,
) -> list[str]:
    """Query the Server and return provider names for online runtimes
    on ``target_daemon_id``.

    The Server's ``list_runtimes`` already filters out stale rows (rows
    whose ``last_seen`` is past the liveness TTL), so we only need to
    filter by ``daemon_id`` and ``status == "online"`` here.
    """
    async def _run() -> list[dict]:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            return await client.list_runtimes()

    runtimes = asyncio.run(_run())
    return [
        r["type"]
        for r in runtimes
        if r.get("daemon_id") == target_daemon_id and r.get("status") == "online"
    ]


def _pick_provider(
    provider: str | None,
    target_daemon_id: str | None,
    server_url: str,
    *,
    local_controller: DaemonController | None = None,
) -> str:
    """Resolve the provider to use for ``run`` / ``chat``.

    Decide-by-context:

    - No ``--daemon`` (local): run the local probe via ``DaemonController``
      (use ``local_controller`` if given, to share the probe cache across
      chat turns). Filter to entries whose binary is available.
    - With ``--daemon``: query the Server's runtime registry for that
      daemon and filter to ``status == "online"`` rows.

    Selection: ``provider`` is None → return the first entry in the
    filtered list (preserving upstream order — probe order locally,
    server-side ``(daemon_id, provider)`` order remotely). If ``provider``
    is given, validate and return it; exit 2 if unavailable.

    Errors out (sys.exit(2)) with a clear list of available providers
    when the explicit one isn't found, or when no candidates exist at all.
    """
    if target_daemon_id is None:
        candidates = _local_candidate_providers(local_controller)
    else:
        candidates = _remote_candidate_providers(target_daemon_id, server_url)

    if provider is not None:
        if provider not in candidates:
            available = ", ".join(sorted(candidates)) or "(none)"
            if target_daemon_id is None:
                ctx = "locally"
                hint = " Run `agt probe` to refresh."
            else:
                ctx = f"on daemon '{target_daemon_id}'"
                hint = ""
            click.echo(
                f"Error: provider '{provider}' is not available {ctx}. "
                f"Available: {available}.{hint}",
                err=True,
            )
            sys.exit(2)
        return provider

    if not candidates:
        if target_daemon_id is None:
            click.echo(
                "Error: no local agents available. "
                "Install one (e.g. `claude`, `codex`) or run `agt probe` to scan.",
                err=True,
            )
        else:
            click.echo(
                f"Error: daemon '{target_daemon_id}' has no online runtimes. "
                f"Is the daemon running? Try "
                f"`agt runtime list --server-url={server_url}`.",
                err=True,
            )
        sys.exit(2)

    return candidates[0]


# ── run ────────────────────────────────────────────────────────────


@cli.command()
@click.option("-p", "--prompt", required=True, help="Prompt to send to the agent.")
@click.option(
    "--provider",
    default=None,
    help="Provider key (e.g. claude, codex, cursor). "
         "Auto-pick first available if omitted "
         "(local probe without --daemon, daemon's runtimes with --daemon).",
)
@click.option(
    "--daemon",
    "target_daemon_id",
    default=None,
    help="Target this daemon via the Server (remote run). Omit to run locally.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
@click.option(
    "--work-dir",
    "work_dir",
    default=None,
    type=click.Path(),
    help="Override the AGT metadata directory (defaults to <cwd>/.agt/tasks/<id>/). "
         "Does NOT change the agent's working directory.",
)
@click.option(
    "--no-work-dir",
    is_flag=True,
    help="Use the legacy daemon-local layout (~/.agt/workspaces/<provider>/<id>/) "
         "for both agent cwd and AGT metadata.",
)
def run(
    prompt: str,
    provider: str | None,
    target_daemon_id: str | None,
    server_url: str,
    work_dir: str | None,
    no_work_dir: bool,
) -> None:
    """Spawn the agent once with the given prompt and stream its output.

    With ``--daemon`` the task is submitted to the Server, pinned to that
    daemon, and polled to completion. Without it, the local agent runs
    directly (no Server round-trip).

    If ``--provider`` is omitted, the first available agent is auto-picked
    (local probe without ``--daemon``; the target daemon's registered
    runtimes otherwise).

    The agent's working directory is always the user's cwd at submit time
    unless ``--no-work-dir`` is given (legacy single-path mode). The AGT
    metadata directory defaults to ``<cwd>/.agt/tasks/<id>/`` and can be
    overridden with ``--work-dir``.
    """
    cwd = Path(os.getcwd())
    # Stable task_id so chat continuation can reuse the same id.
    task_id = uuid.uuid4().hex[:8]
    agt_meta_dir, work_dir_str = _prepare_workdir(cwd, task_id, work_dir, no_work_dir)

    provider = _pick_provider(provider, target_daemon_id, server_url)
    if target_daemon_id is None:
        _run_local(prompt, provider, agent_cwd=cwd, agt_meta_dir=agt_meta_dir)
        return

    click.echo(f"🚀 Submitting to daemon '{target_daemon_id}' ({provider})...\n")
    try:
        data = asyncio.run(_submit_and_wait(
            server_url,
            prompt=prompt,
            provider=provider,
            task_id=task_id,
            target_daemon_id=target_daemon_id,
            work_dir_str=work_dir_str,
        ))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if data is None:
        sys.exit(3)
    _echo_task_result(data)
    sys.exit(0 if data.get("status") == "completed" else 2)


def _chat_turn_local(
    controller: DaemonController,
    provider: str,
    prompt: str,
    task_id: str,
    agent_cwd: Path,
    agt_meta_dir: Path,
) -> None:
    """Run one chat turn against the local daemon."""
    async def _run():
        return await controller.run(
            provider=provider,
            prompt=prompt,
            task_id=task_id,
            agent_cwd=agent_cwd,
            agt_meta_dir=agt_meta_dir,
        )

    result = asyncio.run(_run())
    if result.resume_rejected:
        click.echo("⚠️  Resume was rejected; this turn ran on a fresh session.")
    if result.status != "completed" and result.stderr:
        # Only surface stderr when the agent failed; it's silenced on
        # the live stream otherwise (agent banners live on stderr).
        click.echo(f"\n[stderr] {result.stderr}", nl=False)
        if not result.stderr.endswith("\n"):
            click.echo()


def _chat_turn_remote(
    provider: str,
    prompt: str,
    task_id: str,
    target_daemon_id: str,
    server_url: str,
    work_dir_str: str | None,
) -> None:
    """Run one chat turn pinned to a remote daemon via the Server."""
    async def _remote() -> dict | None:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            await client.submit_task(
                prompt=prompt,
                provider=provider,
                task_id=task_id,
                target_daemon_id=target_daemon_id,
                work_dir=work_dir_str,
            )
            try:
                return await client.wait_task(task_id)
            except TimeoutError:
                click.echo(
                    f"⚠️  No response — is daemon '{target_daemon_id}' online?",
                    err=True,
                )
                return None

    try:
        data = asyncio.run(_remote())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        return

    if data is None:
        return
    stdout = data.get("stdout") or ""
    if stdout:
        click.echo(stdout, nl=not stdout.endswith("\n"))


# ── chat ───────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--provider",
    default=None,
    help="Provider key (e.g. claude, codex, cursor). "
         "Auto-pick first available if omitted "
         "(local probe without --daemon, daemon's runtimes with --daemon).",
)
@click.option(
    "--daemon",
    "target_daemon_id",
    default=None,
    help="Target this daemon via the Server (remote chat). Omit for local.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
@click.option(
    "--work-dir",
    "work_dir",
    default=None,
    type=click.Path(),
    help="Override the AGT metadata directory (defaults to <cwd>/.agt/tasks/<id>/). "
         "Does NOT change the agent's working directory.",
)
@click.option(
    "--no-work-dir",
    is_flag=True,
    help="Use the legacy daemon-local layout (~/.agt/workspaces/<provider>/<id>/) "
         "for both agent cwd and AGT metadata.",
)
def chat(
    provider: str | None,
    target_daemon_id: str | None,
    server_url: str,
    work_dir: str | None,
    no_work_dir: bool,
) -> None:
    """Interactive chat mode with session continuity.

    With ``--daemon`` each turn is submitted to the Server, pinned to that
    daemon, and polled to completion; the shared ``task_id`` keeps the
    remote agent's session alive across turns. Without it, the local agent
    runs directly.

    If ``--provider`` is omitted, the first available agent is auto-picked.

    The agent's working directory is always the user's cwd at chat start
    unless ``--no-work-dir`` is given. The AGT metadata directory defaults
    to ``<cwd>/.agt/tasks/<id>/`` and can be overridden with ``--work-dir``.
    """
    cwd = Path(os.getcwd())
    controller = DaemonController() if target_daemon_id is None else None
    provider = _pick_provider(
        provider, target_daemon_id, server_url, local_controller=controller,
    )
    task_id = f"chat-{uuid.uuid4().hex[:8]}"
    agt_meta_dir, work_dir_str = _prepare_workdir(cwd, task_id, work_dir, no_work_dir)

    mode = f"provider={provider}"
    if target_daemon_id is not None:
        mode += f", daemon={target_daemon_id}"
    click.echo(
        f"🚀 agt chat mode ({mode}, task={task_id}). "
        f"Type 'exit' or 'quit' to leave.\n"
    )

    while True:
        try:
            line = input("agt> ")
        except (EOFError, KeyboardInterrupt):
            click.echo("\n👋 Bye!")
            return

        line = line.strip()
        if not line:
            continue
        if line in ("exit", "quit", ":q"):
            click.echo("👋 Bye!")
            return

        if controller is not None:
            _chat_turn_local(
                controller, provider, line, task_id, cwd, agt_meta_dir,
            )
        else:
            assert target_daemon_id is not None  # else branch ⟹ --daemon given
            _chat_turn_remote(
                provider, line, task_id, target_daemon_id, server_url,
                work_dir_str,
            )


# ── server (Phase 2) ──────────────────────────────────────────────


@cli.group()
def server() -> None:
    """Server-related subcommands."""


@server.command("start")
@click.option(
    "--host",
    default="127.0.0.1",
    show_default=True,
    help="Address to bind.",
)
@click.option(
    "--port",
    default=8420,
    show_default=True,
    type=int,
    help="TCP port to bind.",
)
@click.option(
    "--db-path",
    default=None,
    help="Path to the SQLite database file (default: ~/.agt/agt.db).",
)
def server_start(host: str, port: int, db_path: str | None) -> None:
    """Start the FastAPI Server in the foreground (Ctrl-C to stop)."""
    import argparse

    from ..server.main import cli_main

    args = argparse.Namespace(
        host=host,
        port=port,
        db_path=db_path,
        sessions_json=None,
    )
    sys.exit(cli_main(args))


# ── task (Phase 2) ─────────────────────────────────────────────────


@cli.group()
def task() -> None:
    """Task management commands (talk to the Server)."""


@task.command("submit")
@click.option("-p", "--prompt", required=True)
@click.option("--provider", default="claude", show_default=True)
@click.option(
    "--task-id",
    "user_task_id",
    default=None,
    help="Reuse an existing task_id (for session continuity).",
)
@click.option(
    "--daemon",
    "target_daemon_id",
    default=None,
    help="Pin this task to a specific daemon (free task if omitted).",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def task_submit(
    prompt: str,
    provider: str,
    user_task_id: str | None,
    target_daemon_id: str | None,
    server_url: str,
) -> None:
    """Submit a task to the Server (returns the assigned task_id)."""

    async def _run() -> str:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.submit_task(
                prompt=prompt,
                provider=provider,
                task_id=user_task_id,
                target_daemon_id=target_daemon_id,
            )

    task_id = asyncio.run(_run())
    click.echo(f"task_id={task_id}")
    click.echo(f"  query status with: agt task status {task_id}")


@task.command("status")
@click.argument("task_id")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def task_status(task_id: str, server_url: str) -> None:
    """Query a task's status from the Server."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.task_status(task_id)

    try:
        data = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    _echo_task_result(data)


@task.command("clean")
@click.argument("task_id", required=False)
@click.option(
    "--all-done",
    "all_done",
    is_flag=True,
    help="Remove work_dir for every completed/failed/timeout task.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Allow removal even when the task is still running.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def task_clean(
    task_id: str | None,
    all_done: bool,
    force: bool,
    server_url: str,
) -> None:
    """Remove a task's local AGT metadata directory.

    Reads the task's ``work_dir`` from the Server (Phase 4: cwd-derived,
    e.g. ``<cwd>/.agt/tasks/<id>/``) and ``shutil.rmtree``'s it. Refuses
    to touch running tasks unless ``--force`` is given, and warns (exit 4)
    when the task has no ``work_dir`` — i.e. it belongs to a remote
    daemon whose filesystem we don't share.
    """

    async def _fetch(ids: list[str]) -> list[dict]:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            if ids:
                return [await client.task_status(i) for i in ids]
            return await client.list_tasks(
                statuses=["completed", "failed", "timeout"],
            )

    if not task_id and not all_done:
        click.echo("Error: provide a TASK_ID or pass --all-done", err=True)
        sys.exit(2)

    try:
        tasks = asyncio.run(_fetch([task_id] if task_id else []))
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not tasks:
        click.echo("No matching tasks.", err=True)
        sys.exit(4)

    failures = sum(1 for t in tasks if not _clean_one_task(t, force))
    if failures:
        sys.exit(4)


def _clean_one_task(task: dict, force: bool) -> bool:
    """Best-effort clean of one task's work_dir. Returns True on success."""
    tid = task["task_id"]
    status = task.get("status")
    work_dir = task.get("work_dir")
    if status == "running" and not force:
        click.echo(
            f"⚠️  {tid}: status=running, refusing to clean (use --force).",
            err=True,
        )
        return False
    if not work_dir:
        click.echo(
            f"⚠️  {tid}: no work_dir (remote daemon task). Skipping.",
            err=True,
        )
        return False
    path = Path(work_dir)
    if not path.exists():
        click.echo(f"⚠️  {tid}: {path} does not exist. Skipping.", err=True)
        return True  # already clean — not a failure
    try:
        shutil.rmtree(path)
    except OSError as exc:
        click.echo(f"❌  {tid}: failed to remove {path}: {exc}", err=True)
        return False
    click.echo(f"🧹 {tid}: removed {path}")
    return True


# ── runtime (Phase 3.4: registry queries) ──────────────────────────


@cli.group()
def runtime() -> None:
    """Runtime registry queries (talk to the Server)."""


@runtime.command("list")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def runtime_list(server_url: str) -> None:
    """List all runtimes registered by every daemon on the Server."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_runtimes()

    try:
        runtimes = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not runtimes:
        click.echo("No runtimes registered. Start a daemon with `agt daemon start`.")
        return

    click.echo(
        f"📡 {len(runtimes)} runtime(s) registered on the Server\n"
    )
    click.echo(
        f"  {'daemon_id':<12} {'provider':<12} {'version':<12} "
        f"{'device':<24} {'status':<8}"
    )
    click.echo("  " + "─" * 72)
    for r in runtimes:
        click.echo(
            f"  {r['daemon_id']:<12} {r['type']:<12} {r['version']:<12} "
            f"{r['device_name']:<24} {r['status']:<8}"
        )


# ── daemon (Phase 2 real implementation) ────────────────────────────


@cli.group()
def daemon() -> None:
    """Daemon-related subcommands."""


@daemon.command("start")
@click.option("--server-url", default=None, help="Override AGT_SERVER_URL.")
def daemon_start(server_url: str | None) -> None:
    """Start the background daemon (connects to Server, registers runtimes)."""
    import argparse
    from ..daemon.main import cmd_start

    args = argparse.Namespace(server_url=server_url)
    sys.exit(cmd_start(args))


@daemon.command("stop")
def daemon_stop() -> None:
    """Signal the running daemon to stop."""
    import argparse
    from ..daemon.main import cmd_stop

    args = argparse.Namespace()
    sys.exit(cmd_stop(args))


@daemon.command("status")
def daemon_status() -> None:
    """Show whether the daemon is currently running."""
    import argparse
    from ..daemon.main import cmd_status

    args = argparse.Namespace()
    sys.exit(cmd_status(args))


# ── token (Phase 3, dev/debug) ──────────────────────────────────────


@cli.group()
def token() -> None:
    """Issue / inspect task-scoped HMAC tokens.

    Development and debugging aid — the daemon normally injects AGT_TASK_TOKEN
    automatically when it spawns an agent subprocess. Use these commands to
    hand-craft tokens for testing the Server's ``/api/agent/*`` endpoints
    from a curl prompt or a notebook.
    """


@token.command("issue")
@click.option(
    "--task-id",
    required=True,
    help="The task_id this token should be bound to (must match X-Task-Id).",
)
@click.option(
    "--provider",
    default="claude",
    show_default=True,
    help="Provider key — embedded in the token's ``sub`` claim.",
)
@click.option(
    "--workspace",
    default=None,
    help="Workspace id (default: /tmp/agt/<task-id>).",
)
@click.option(
    "--ttl-hours",
    default=24,
    show_default=True,
    type=int,
    help="Token validity in hours from now.",
)
@click.option(
    "--agent-id",
    default="cli-issue",
    show_default=True,
    help="Agent identifier embedded in ``sub`` (default: cli-issue).",
)
def token_issue(
    task_id: str,
    provider: str,
    workspace: str | None,
    ttl_hours: int,
    agent_id: str,
) -> None:
    """Issue a task-scoped token signed with AGT_SERVER_SECRET.

    Prints the token to stdout; the Server will accept it on requests that
    carry ``X-Task-Token: <token>`` and ``X-Task-Id: <task-id>``.
    """
    secret = os.environ.get("AGT_SERVER_SECRET", "")
    if not secret:
        click.echo(
            "Error: AGT_SERVER_SECRET env var is required to issue tokens "
            "(generate with: openssl rand -hex 32).",
            err=True,
        )
        sys.exit(1)

    workspace_id = workspace or f"/tmp/agt/{task_id}"
    issued = TaskToken.create(
        agent_id=agent_id,
        task_id=task_id,
        workspace_id=workspace_id,
        secret=secret,
        ttl_hours=ttl_hours,
    )
    click.echo(issued)


__all__ = ["cli"]