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
import signal
import sys
import uuid

import click

from ..comm import ServerClient
from ..daemon.daemon import Daemon as DaemonController
from ..server.services.auth_service import TaskToken


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


# ── run ────────────────────────────────────────────────────────────


@cli.command()
@click.option("-p", "--prompt", required=True, help="Prompt to send to the agent.")
@click.option(
    "--provider",
    default="claude",
    show_default=True,
    help="Provider key (e.g. claude, codex, cursor).",
)
def run(prompt: str, provider: str) -> None:
    """Spawn the agent once with the given prompt and stream its output."""
    controller = DaemonController()
    click.echo(f"🚀 Executing with {provider}...\n")

    async def _run():
        return await controller.run(
            provider=provider, prompt=prompt, interactive=True,
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
    sys.exit(0 if result.status == "completed" else 2)


# ── chat ───────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--provider",
    default="claude",
    show_default=True,
    help="Provider key (e.g. claude, codex, cursor).",
)
def chat(provider: str) -> None:
    """Interactive chat mode with session continuity."""
    controller = DaemonController()
    task_id = f"chat-{uuid.uuid4().hex[:8]}"
    click.echo(
        f"🚀 agt chat mode (provider={provider}, "
        f"task={task_id}). Type 'exit' or 'quit' to leave.\n"
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

        async def _run():
            return await controller.run(
                provider=provider,
                prompt=line,
                task_id=task_id,
                interactive=True,
            )

        result = asyncio.run(_run())
        if result.resume_rejected:
            click.echo(
                "⚠️  Resume was rejected; this turn ran on a fresh session."
            )
        click.echo(
            f"\n[exit_code={result.exit_code}, duration={result.duration:.1f}s]\n"
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
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def task_submit(
    prompt: str,
    provider: str,
    user_task_id: str | None,
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

    stdout = data.get("stdout") or ""
    stderr = data.get("stderr") or ""

    click.echo(
        f"task_id   : {data['task_id']}\n"
        f"provider  : {data['provider']}\n"
        f"status    : {data['status']}\n"
        f"session_id: {data['session_id'] or '—'}\n"
        f"exit_code : {data['exit_code']}\n"
        f"duration  : {data['duration']:.2f}s"
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