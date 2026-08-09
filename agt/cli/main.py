"""``agt`` CLI entry point — Click-based command surface."""

from __future__ import annotations

import asyncio
import sys
import uuid

import click

from ..daemon.daemon import Daemon as DaemonController


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
        return await controller.run(provider=provider, prompt=prompt)

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
        f"🚀 agt chat mode (provider={provider}, task={task_id}). "
        "Type 'exit' or 'quit' to leave.\n"
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
                provider=provider, prompt=line, task_id=task_id
            )

        result = asyncio.run(_run())
        if result.resume_rejected:
            click.echo(
                "⚠️  Resume was rejected; this turn ran on a fresh session."
            )
        click.echo(
            f"\n[exit_code={result.exit_code}, duration={result.duration:.1f}s]\n"
        )


# ── daemon (Phase 1 placeholder) ──────────────────────────────────


@cli.group()
def daemon() -> None:
    """Daemon-related subcommands."""


@daemon.command("start")
def daemon_start() -> None:
    """Start the persistent daemon (Phase 2 only — placeholder for now)."""
    click.echo(
        "⚠️  `agt daemon start` is a Phase 2 feature. "
        "In Phase 1, `agt run` and `agt chat` work without a background daemon."
    )


__all__ = ["cli"]