"""``agt goal`` — dynamic orchestration commands (goal mode).

The user hands one requirement to a root agent; that agent decomposes,
spawns child agents recursively (within budget), and synthesizes the
final answer. Everything runs on the existing Job-tree engine — these
commands are the entry + observability layer.
"""

from __future__ import annotations

import asyncio
import time
import sys
from pathlib import Path

import click

from ..comm import ServerClient
from ..comm.errors import RPCRejected

_TERMINAL = {"done", "failed", "cancelled"}

_SYMBOLS = {
    "queued": "○",
    "running": "◐",
    "waiting_children": "◑",
    "waiting_input": "⏸",
    "done": "●",
    "failed": "✕",
    "cancelled": "⊘",
    "blocked": "▣",
}


@click.group()
def goal() -> None:
    """Dynamic orchestration: one goal, one root agent, recursive spawn."""


def _goal_options(f):
    for args, kwargs in (
        (("--identity",), {"default": None, "help": "Root orchestrator identity DID."}),
        (("--provider",), {"default": None, "help": "Provider when no identity is given."}),
        (("--daemon",), {"default": None, "help": "Pin the root to one daemon."}),
        (("--dir",), {"default": None, "type": click.Path(), "help": "Root agent working directory."}),
        (("--max-depth",), {"default": 5, "show_default": True, "help": "Max tree depth (root=1)."}),
        (("--max-descendants",), {"default": 64, "show_default": True, "help": "Max non-root descendants under the root."}),
        (("--server-url",), {"default": "http://127.0.0.1:8420", "show_default": True}),
    ):
        f = click.option(*args, **kwargs)(f)
    return f


def _submit_kwargs(kwargs: dict) -> dict:
    return {
        "identity_did": kwargs.get("identity"),
        "provider": kwargs.get("provider"),
        "daemon_id": kwargs.get("daemon"),
        "dir": kwargs.get("dir"),
        "max_depth": kwargs.get("max_depth"),
        "max_descendants": kwargs.get("max_descendants"),
    }


@goal.command("submit")
@click.argument("goal_text")
@_goal_options
def goal_submit(goal_text: str, **kwargs) -> None:
    """Submit a goal root Job; prints the root_job_id."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=kwargs["server_url"], daemon_id="cli-goal"
        ) as client:
            return await client.submit_goal(goal_text, **_submit_kwargs(kwargs))

    result = asyncio.run(_run_or_die(_run()))
    click.echo(f"root_job_id: {result['root_job_id']}")


@goal.command("run")
@click.argument("goal_text")
@click.option("--interval", default=2.0, show_default=True, help="Poll interval (s).")
@click.option("--timeout", default=None, type=float, help="Wall-clock watchdog (s); cancels on expiry.")
@_goal_options
def goal_run(goal_text: str, interval: float, timeout: float | None, **kwargs) -> None:
    """Submit a goal and follow its tree until the root is terminal."""

    async def _run() -> int:
        async with ServerClient(
            base_url=kwargs["server_url"], daemon_id="cli-goal"
        ) as client:
            submitted = await client.submit_goal(goal_text, **_submit_kwargs(kwargs))
            root = submitted["root_job_id"]
            click.echo(f"root_job_id: {root}")
            started = time.monotonic()
            announced: set[str] = set()
            while True:
                tree = await client.job_tree(root)
                node = tree["node"]
                click.echo()
                _render_tree(node)
                for waiting_id in _waiting_input_ids(node):
                    if waiting_id not in announced:
                        announced.add(waiting_id)
                        click.echo(
                            f"\n[goal] {waiting_id} needs input — answer with: "
                            f'agt goal input {waiting_id} "你的回答"'
                        )
                if node["status"] in _TERMINAL:
                    await _print_final(client, root, node)
                    return 0 if node["status"] == "done" else 1
                if timeout is not None and time.monotonic() - started > timeout:
                    click.echo(f"[goal] timeout ({timeout}s) — cancelling", err=True)
                    await client.job_cancel(root)
                    return 1
                await asyncio.sleep(interval)

    sys.exit(asyncio.run(_run_or_die(_run())))


async def _print_final(client: ServerClient, root: str, node: dict) -> None:
    log = await client.job_log(root)
    steps = log.get("steps") or []
    stdout = next(
        (s.get("stdout") or "" for s in reversed(steps) if s.get("stdout")), ""
    )
    if stdout:
        click.echo("\n— final answer (root) —")
        click.echo(stdout, nl=False)
        if not stdout.endswith("\n"):
            click.echo()
    artifacts: list[str] = []

    def _collect(n: dict) -> None:
        if n.get("status") == "done":
            for art in n.get("artifacts") or []:
                artifacts.append(f"  {n.get('label') or n['job_id'][:8]}: {art['uri']}  {art.get('name') or ''}")
        for child in n.get("children") or []:
            _collect(child)

    _collect(node)
    if artifacts:
        click.echo("\n— artifacts —")
        click.echo("\n".join(artifacts))


@goal.command("log")
@click.argument("root_job_id")
@click.argument("job_id", required=False)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_log(root_job_id: str, job_id: str | None, server_url: str) -> None:
    """Show full stdout/stderr for one node (default: the root)."""

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            return await client.job_log(job_id or root_job_id)

    log = asyncio.run(_run_or_die(_run()))
    click.echo(f"job_id    : {log['job_id']}")
    click.echo(f"status    : {log['status']}")
    steps = log.get("steps") or []
    if not steps:
        click.echo("\n(no steps yet)")
        return
    for step in steps:
        click.echo(f"\n--- step {step['task_id']} ({step['status']}) ---")
        if step.get("session_id"):
            click.echo(f"session_id: {step['session_id']}")
        for key in ("stdout", "stderr"):
            text = step.get(key) or ""
            if text:
                click.echo(f"--- {key} ---\n{text}", nl=False)
                if not text.endswith("\n"):
                    click.echo()
                click.echo(f"--- end {key} ---")


@goal.command("status")
@click.argument("root_job_id")
@click.option("--flat", is_flag=True, help="Print one line per node (machine-friendly).")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_status(root_job_id: str, flat: bool, server_url: str) -> None:
    """Render the Job subtree snapshot for a goal run."""

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            return await client.job_tree(root_job_id)

    tree = asyncio.run(_run_or_die(_run()))
    click.echo(f"root_job_id: {tree['root_job_id']}")
    if flat:
        _render_flat(tree["node"], depth=0)
    else:
        _render_tree(tree["node"])


@goal.command("input")
@click.argument("job_id")
@click.argument("answer")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_input(job_id: str, answer: str, server_url: str) -> None:
    """Answer a waiting_input node (append the next Step → resumes to running)."""

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            return await client.job_input(job_id, answer)

    result = asyncio.run(_run_or_die(_run()))
    click.echo(f"job_id: {result['job_id']}")
    click.echo(f"status: {result['status']}")


@goal.command("cancel")
@click.argument("root_job_id")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_cancel(root_job_id: str, server_url: str) -> None:
    """Cancel a goal run and its whole subtree (cascade)."""

    async def _run() -> tuple[bool, int]:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            tree = await client.job_tree(root_job_id)
            total = _count_nodes(tree["node"])
            ok = await client.job_cancel(root_job_id)
            return ok, total

    ok, total = asyncio.run(_run_or_die(_run()))
    if ok:
        click.echo(f"cancelled {root_job_id} (subtree: {total} jobs)")
    else:
        click.echo(f"cancel was not accepted for {root_job_id}", err=True)
        sys.exit(1)


@goal.command("artifacts")
@click.argument("root_job_id")
@click.option("--out-dir", type=click.Path(), default=".", show_default=True,
              help="Directory to write downloaded artifacts into.")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_artifacts(root_job_id: str, out_dir: str, server_url: str) -> None:
    """Download every artifact in a goal tree into --out-dir."""

    async def _run() -> list[tuple[str, str, str]]:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            tree = await client.job_tree(root_job_id)
            return _collect_artifacts(tree["node"])

    artifacts = asyncio.run(_run_or_die(_run()))
    if not artifacts:
        click.echo("(no artifacts in tree)")
        return
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    used: set[str] = set()
    for label, artifact_id, name in artifacts:
        target = name or f"{artifact_id}"
        if target in used:
            target = f"{label}-{target}"
        used.add(target)
        path = out / target
        try:
            # Re-download within the same connection context so the CLI
            # owns exactly one session per invocation.
            async def _download() -> bytes:
                async with ServerClient(
                    base_url=server_url, daemon_id="cli-goal"
                ) as client:
                    return await client.get_artifact_content(artifact_id)

            data = asyncio.run(_run_or_die(_download()))
            path.write_bytes(data)
            click.echo(f"{path}  ({len(data)} bytes)")
        except Exception as exc:
            click.echo(f"failed {artifact_id}: {exc}", err=True)


@goal.command("export")
@click.argument("root_job_id")
@click.option("-o", "--out", type=click.Path(), default=None,
              help="Write YAML to FILE instead of stdout.")
@click.option("--include-failed", is_flag=True, help="Export failed retry waves too.")
@click.option("--keep-planning", is_flag=True, help="Keep pure planning rounds (orphan tasks).")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
)
def goal_export(
    root_job_id: str,
    out: str | None,
    include_failed: bool,
    keep_planning: bool,
    server_url: str,
) -> None:
    """Export a goal run's Step-event graph as a SOP YAML draft."""

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-goal") as client:
            return await client.goal_export(
                root_job_id,
                include_failed=include_failed,
                keep_planning=keep_planning,
            )

    result = asyncio.run(_run_or_die(_run()))
    yaml_text = result.get("yaml") or ""
    if out:
        Path(out).write_text(yaml_text)
        click.echo(f"wrote {out}")
    else:
        click.echo(yaml_text, nl=False)

    warnings = result.get("warnings") or []
    if warnings:
        click.echo("[goal export] warnings:", err=True)
        for w in warnings:
            click.echo(f"  - {w}", err=True)
    click.echo(
        "\n提示: 审阅草案后，用 `agt sop validate <file>` 校验、"
        "`agt sop run <file>` 复用。",
        err=True,
    )


# ── rendering ──────────────────────────────────────────────────────


def _symbol(status: str) -> str:
    return _SYMBOLS.get(status, "·")


def _fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def _count_nodes(node: dict) -> int:
    return 1 + sum(_count_nodes(c) for c in node.get("children") or [])


def _waiting_input_ids(node: dict) -> list[str]:
    ids: list[str] = []
    if node.get("status") == "waiting_input":
        ids.append(node["job_id"])
    for child in node.get("children") or []:
        ids.extend(_waiting_input_ids(child))
    return ids


def _collect_artifacts(node: dict) -> list[tuple[str, str, str]]:
    """Flatten the tree into ``(label, artifact_id, name)`` triplets."""
    out: list[tuple[str, str, str]] = []
    label = (node.get("label") or node["job_id"][:8])[:24]
    for art in node.get("artifacts") or []:
        out.append((label, art["artifact_id"], art.get("name") or ""))
    for child in node.get("children") or []:
        out.extend(_collect_artifacts(child))
    return out


def _node_line(node: dict, name: str) -> str:
    where = node.get("provider") or "—"
    daemon = node.get("daemon_id")
    if daemon:
        where = f"{where}@{daemon}"
    status = node.get("status") or "—"
    label = node.get("label") or ""
    return (
        f"{name:<20} {_symbol(status)} {status:<17} {where:<22} "
        f"{_fmt_duration(node.get('duration')):>7}  {label}"
    )


def _render_tree(node: dict, prefix: str = "", name: str = "goal") -> None:
    click.echo(prefix + _node_line(node, name))
    error = node.get("error")
    if error:
        indent = " " * len(prefix)
        click.echo(f"{indent}↳ error: {error}")
    children = node.get("children") or []
    for i, child in enumerate(children):
        child_last = i == len(children) - 1
        branch = "└─ " if child_last else "├─ "
        _render_tree(
            child,
            prefix + branch,
            (child.get("label") or child["job_id"][:8])[:20],
        )


def _render_flat(node: dict, depth: int) -> None:
    fields = [
        str(depth),
        node["job_id"],
        node.get("status") or "—",
        f"{node.get('provider') or '—'}@{node.get('daemon_id') or '—'}",
        _fmt_duration(node.get("duration")),
        node.get("label") or "",
    ]
    if node.get("error"):
        fields.append(str(node["error"]))
    click.echo("  ".join(fields))
    for child in node.get("children") or []:
        _render_flat(child, depth + 1)


async def _run_or_die(coro):
    try:
        return await coro
    except RPCRejected as exc:
        click.echo(f"Error: server rejected: {exc.body}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)


__all__ = ["goal"]
