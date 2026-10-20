"""``agt sop`` — static DAG pipeline commands (Phase 5)."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click

from ..comm import ServerClient
from ..comm.errors import RPCRejected
from ..sop.dsl import SOPError, parse_sop

_TERMINAL = {"done", "failed", "cancelled"}


def _truncate(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    return text[: width - 1] + "…"


@click.group()
def sop() -> None:
    """SOP static-DAG pipelines (validate / submit / run / status)."""


@sop.command("validate")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
def sop_validate(file: Path) -> None:
    """Parse + statically validate a SOP YAML/JSON file (no submission)."""
    try:
        spec = parse_sop(file.read_text(encoding="utf-8"))
    except SOPError as exc:
        click.echo(f"invalid: {exc}", err=True)
        sys.exit(1)
    click.echo(
        f"ok: {len(spec.nodes)} node(s), {len(spec.edges)} edge(s), "
        f"root candidates={[n.name for n in spec.nodes if not n.depends_on][0]}"
    )


@sop.command("submit")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--server-url", default="http://127.0.0.1:8420", show_default=True)
def sop_submit(file: Path, server_url: str) -> None:
    """Validate, expand, and submit a SOP; prints the root job id."""
    spec = _parse_or_die(file)
    cwd, local_daemon_id = _submit_context()

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-sop") as client:
            return await client.submit_sop(
                spec, cwd=cwd, local_daemon_id=local_daemon_id
            )

    result = asyncio.run(_run_or_die(_run()))
    click.echo(f"root_job_id: {result['root_job_id']}")
    for name, job_id in result["nodes"].items():
        click.echo(f"  {name}: {job_id}")


@sop.command("run")
@click.argument("file", type=click.Path(exists=True, path_type=Path))
@click.option("--server-url", default="http://127.0.0.1:8420", show_default=True)
@click.option("--interval", default=2.0, show_default=True, help="Poll interval (s).")
def sop_run(file: Path, server_url: str, interval: float) -> None:
    """Submit a SOP and follow it until every DAG node is terminal."""
    spec = _parse_or_die(file)
    cwd, local_daemon_id = _submit_context()

    async def _run() -> int:
        async with ServerClient(base_url=server_url, daemon_id="cli-sop") as client:
            submitted = await client.submit_sop(
                spec, cwd=cwd, local_daemon_id=local_daemon_id
            )
            root = submitted["root_job_id"]
            click.echo(f"root_job_id: {root}")
            while True:
                st = await client.sop_status(root)
                _render(st)
                if st["all_terminal"]:
                    click.echo("— terminal artifacts —")
                    for node in st["nodes"]:
                        if node["status"] == "done":
                            for uri in node["artifacts"]:
                                click.echo(f"  {node['name']}: {uri}")
                    failed = [n["name"] for n in st["nodes"] if n["status"] != "done"]
                    return 1 if failed else 0
                await asyncio.sleep(interval)

    sys.exit(asyncio.run(_run_or_die(_run())))


@sop.command("runs")
@click.option("--daemon", "daemon_id", default=None, help="Filter to one daemon.")
@click.option("--server-url", default="http://127.0.0.1:8420", show_default=True)
@click.option("--limit", default=50, show_default=True, help="Max runs to show.")
@click.option("--offset", default=0, show_default=True, help="Pagination offset.")
def sop_runs(
    daemon_id: str | None,
    server_url: str,
    limit: int,
    offset: int,
) -> None:
    """List SOP runs for the current cwd."""
    cwd = str(Path.cwd())

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-sop") as client:
            return await client.list_runs(
                mode="sop",
                cwd=cwd,
                daemon_id=daemon_id,
                limit=limit,
                offset=offset,
            )

    result = asyncio.run(_run_or_die(_run()))
    runs = result.get("runs", [])
    total = result.get("total", 0)
    if not runs:
        label = f"on daemon '{daemon_id}'" if daemon_id else "in current cwd"
        click.echo(f"No SOP runs {label}.")
        return
    click.echo(f"🧩 {len(runs)}/{total} SOP run(s) for {cwd}\n")
    rows = [
        (
            r.get("root_job_id", ""),
            r.get("status", ""),
            r.get("provider", ""),
            r.get("daemon_id") or "—",
            _truncate(r.get("title") or "", 40),
            r.get("updated_at", ""),
        )
        for r in runs
    ]
    widths = [max(len(r[i]) for r in rows) for i in range(6)]
    header = (
        f"  {'ROOT':<{widths[0]}}  {'STATUS':<{widths[1]}}  {'PROVIDER':<{widths[2]}}  "
        f"{'DAEMON':<{widths[3]}}  {'NAME':<{widths[4]}}  UPDATED"
    )
    click.echo(header)
    click.echo("  " + "─" * (sum(widths) + 14))
    for r in rows:
        click.echo(
            f"  {r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  {r[2]:<{widths[2]}}  "
            f"{r[3]:<{widths[3]}}  {r[4]:<{widths[4]}}  {r[5]}"
        )


@sop.command("status")
@click.argument("root_job_id")
@click.option("--server-url", default="http://127.0.0.1:8420", show_default=True)
def sop_status(root_job_id: str, server_url: str) -> None:
    """Show per-node status of a submitted SOP DAG."""
    async def _run() -> None:
        async with ServerClient(base_url=server_url, daemon_id="cli-sop") as client:
            _render(await client.sop_status(root_job_id))

    asyncio.run(_run_or_die(_run()))


@sop.command("log")
@click.argument("root_job_id")
@click.argument("node_name")
@click.option("--server-url", default="http://127.0.0.1:8420", show_default=True)
def sop_log(root_job_id: str, node_name: str, server_url: str) -> None:
    """Show full stdout/stderr for one SOP node (addressed by name)."""
    async def _run() -> None:
        async with ServerClient(base_url=server_url, daemon_id="cli-sop") as client:
            _render_log(await client.sop_node_log(root_job_id, node_name))

    asyncio.run(_run_or_die(_run()))


def _parse_or_die(file: Path) -> dict:
    try:
        # Return the raw document — the server re-validates via parse_sop.
        import yaml

        data = yaml.safe_load(file.read_text(encoding="utf-8"))
        parse_sop(data if isinstance(data, dict) else {})
        return data
    except SOPError as exc:
        click.echo(f"invalid: {exc}", err=True)
        sys.exit(1)


def _submit_context() -> tuple[str, str | None]:
    """Return ``(submitter pwd, local daemon id)`` for agent_cwd defaulting.

    ``_local_daemon_id`` is imported lazily: ``main`` imports this module
    at the bottom of its own body, so a top-level import would cycle.
    """
    from .main import _local_daemon_id  # noqa: PLC0415

    return str(Path.cwd()), _local_daemon_id()


def _render(st: dict) -> None:
    for node in st["nodes"]:
        sid = node.get("session_id") or "—"
        prov = node.get("provider") or "—"
        name = node["name"]
        if node.get("iteration"):
            name = f"{name}#{node['iteration']}"
        if node.get("loop_name"):
            name = f"{name} ({node['loop_name']})"
        deps = f"  (← {','.join(node['depends_on'])})" if node["depends_on"] else ""
        arts = f"  [{len(node['artifacts'])} artifact(s)]" if node["artifacts"] else ""
        click.echo(
            f"{name:<30} {node['status']:<12} {prov:<10} {sid:<36}{deps}{arts}"
        )


def _render_log(log: dict) -> None:
    click.echo(f"node      : {log['node_name']}")
    click.echo(f"job_id    : {log['job_id']}")
    click.echo(f"provider  : {log.get('provider') or '—'}")
    click.echo(f"status    : {log['status']}")

    steps = log.get("steps") or []
    if not steps:
        click.echo("\n(no steps yet — node not activated)")
        return

    current_iteration = None
    for step in steps:
        iteration = step.get("iteration") or 0
        if iteration != current_iteration:
            current_iteration = iteration
            label = f"iteration {iteration}" if iteration else "non-loop"
            click.echo(f"\n=== {label} ===")
        click.echo(f"\n--- step {step['task_id']} ({step['status']}) ---")
        if step.get("session_id"):
            click.echo(f"session_id: {step['session_id']}")
        stdout = step.get("stdout") or ""
        stderr = step.get("stderr") or ""
        if stdout:
            click.echo(f"--- stdout ---\n{stdout}", nl=False)
            if not stdout.endswith("\n"):
                click.echo()
            click.echo("--- end stdout ---")
        if stderr:
            click.echo(f"--- stderr ---\n{stderr}", nl=False)
            if not stderr.endswith("\n"):
                click.echo()
            click.echo("--- end stderr ---")


async def _run_or_die(coro):
    try:
        return await coro
    except RPCRejected as exc:
        click.echo(f"Error: server rejected: {exc.body}", err=True)
        sys.exit(1)


__all__ = ["sop"]
