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
import base64
import json
import mimetypes
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import click
import httpx

from ..comm import RPCRejected, ServerClient
from ..daemon.daemon import Daemon as DaemonController
from ..runtime.models import AgentEntry
from ..server.services.auth_service import TaskToken
from ..server.services.identity_service import normalize_identity_did
from ..task.session import CwdSessionStore
from ..agent_history import Message, load_history


# ── workdir helpers (Phase 4) ──────────────────────────────────────


class CLIError(RuntimeError):
    """Raised by CLI helpers to surface user-visible errors with sys.exit."""


def _coerce_identity_did(identity_did: str | None) -> str | None:
    """Normalize ``--identity``; exit cleanly on an invalid value.

    Keeps a typo'd value (e.g. ``id:agt:...``) from reaching the server,
    where it would surface as a cryptic ``identity_not_found`` 404. Accepts
    a full ``did:agt:<slug>`` or a bare slug; anything else is a usage error.
    """
    if identity_did is None:
        return None
    try:
        return normalize_identity_did(identity_did)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)


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
    identity_did: str | None = None,
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
            identity_did=identity_did,
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
@click.option(
    "--identity", "identity_did", default=None,
    help="Identity for this task. Requires --daemon so the Server can "
         "resolve a live runtime. Bindings are optional routing "
         "preferences; they do not restrict which provider may use the "
         "identity.",
)
def run(
    prompt: str,
    provider: str | None,
    target_daemon_id: str | None,
    server_url: str,
    work_dir: str | None,
    no_work_dir: bool,
    identity_did: str | None,
) -> None:
    if identity_did is not None and target_daemon_id is None:
        # Identity resolution requires Server runtime discovery; refuse
        # the direct local fast-path rather than silently skipping it.
        click.echo(
            "Error: --identity requires --daemon "
            "(identity runtime resolution happens on the Server).",
            err=True,
        )
        sys.exit(2)
    identity_did = _coerce_identity_did(identity_did)
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
            identity_did=identity_did,
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
    *,
    resume_session_id: str | None = None,
    first_prompt: str | None = None,
    turn_index: int = 1,
    files: tuple[Path, ...] = (),
    event_printer: _AgentEventPrinter | None = None,
) -> None:
    """Run one chat turn against the local daemon.

    ``files`` never touch the Server on this path: the local controller
    already runs on this machine, so each attachment is passed as a
    zero-copy ``local_path`` hint and rendered to ``Input file: <path>``
    by the shared message renderer (same prompt shape as remote turns).

    ``event_printer`` receives structured thinking/tool events live via
    the executor's ``event_cb`` hook.
    """
    async def _run():
        effective_prompt = prompt
        if files:
            from ..task.message_render import render_task_message

            message = {
                "role": "user",
                "parts": [
                    {"type": "text", "text": prompt},
                    *[
                        {
                            "type": "file",
                            "uri": path.resolve().as_uri(),
                            "name": path.name,
                            "mime_type": mimetypes.guess_type(path.name)[0],
                            "local_path": str(path.resolve()),
                        }
                        for path in files
                    ],
                ],
            }
            effective_prompt = await render_task_message(
                message, agent_cwd=agent_cwd
            )
        return await controller.run(
            provider=provider,
            prompt=effective_prompt,
            task_id=task_id,
            agent_cwd=agent_cwd,
            agt_meta_dir=agt_meta_dir,
            resume_session_id=resume_session_id,
            first_prompt=first_prompt,
            turn_index=turn_index,
            use_cwd_session_store=True,
            event_cb=event_printer,
        )

    result = asyncio.run(_run())
    if event_printer is not None:
        event_printer.usage_from_done({"usage": result.usage})
    # Streaming (Phase 5): each text block is printed with
    # ``end=""``, so the terminal cursor may sit mid-line when the
    # turn ends. Make sure the next ``agt> `` prompt lands on a fresh
    # line — ``result.stdout`` mirrors what we printed, so we use it
    # to decide whether a trailing newline is needed.
    if result.stdout and not result.stdout.endswith("\n"):
        click.echo()
    if result.resume_rejected:
        click.echo("⚠️  Resume was rejected; this turn ran on a fresh session.")
    if result.status != "completed" and result.stderr:
        # Only surface stderr when the agent failed; it's silenced on
        # the live stream otherwise (agent banners live on stderr).
        click.echo(f"\n[stderr] {result.stderr}", nl=False)
        if not result.stderr.endswith("\n"):
            click.echo()


def _finish_stream_line(stdout: str) -> None:
    """Emit the trailing newline if the streamed text didn't end with one.

    Streamed chunks are printed with ``nl=False`` so the cursor can sit
    mid-line; once the turn is done we mirror ``_chat_turn_local`` and land
    the next ``agt> `` prompt on a fresh line.
    """
    if stdout and not stdout.endswith("\n"):
        click.echo()


class _AgentEventPrinter:
    """Render structured agent events on the terminal during a chat turn.

    Text still arrives through the plain-text stream; this only adds the
    opt-in extras. Defaults are all-off so existing terminal output is
    byte-identical to the pre-event behaviour.
    """

    def __init__(
        self,
        *,
        show_tools: bool = False,
        show_thinking: bool = False,
        show_usage: bool = False,
    ) -> None:
        self.show_tools = show_tools
        self.show_thinking = show_thinking
        self.show_usage = show_usage
        self._tool_names: dict[str, str] = {}
        self._printed_usage = False

    def __call__(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if kind == "thinking_start" and self.show_thinking:
            click.echo("\n💭 ", nl=False)
        elif kind == "thinking_delta" and self.show_thinking:
            text = event.get("text")
            if isinstance(text, str) and text:
                click.echo(text, nl=False)
        elif kind == "thinking_end" and self.show_thinking:
            click.echo()
        elif kind == "tool_use_start" and self.show_tools:
            name = event.get("name") or "tool"
            tool_use_id = event.get("tool_use_id")
            if isinstance(tool_use_id, str):
                self._tool_names[tool_use_id] = name
            click.echo(f"\n🔧 {name}", nl=False)
        elif kind == "tool_use_end" and self.show_tools:
            summary = _tool_input_summary(event.get("input"))
            if summary:
                click.echo(f": {summary}", nl=False)
            click.echo()
        elif kind == "tool_result" and self.show_tools:
            content = str(event.get("content") or "")
            first_line = content.strip().splitlines()[0] if content.strip() else ""
            if len(first_line) > 120:
                first_line = f"{first_line[:117]}…"
            marker = "✗" if event.get("is_error") else "↳"
            click.echo(f"   {marker} {first_line}")
        elif (
            kind == "usage"
            and self.show_usage
            and event.get("scope") == "turn"
        ):
            self._printed_usage = True
            click.echo(f"\n{_format_usage_line(event)}")

    def usage_from_done(self, done: dict) -> None:
        """Print the usage summary off a ``done`` frame when no event had it.

        Old servers push ``usage`` only on the terminal frame; new ones also
        send a ``usage`` event. This keeps the footer exactly once.
        """
        if not self.show_usage or self._printed_usage:
            return
        usage = done.get("usage")
        if not isinstance(usage, dict):
            return
        model = done.get("model")
        if model and not usage.get("model"):
            usage = {**usage, "model": model}
        self._printed_usage = True
        click.echo(f"\n{_format_usage_line(usage)}")


def _tool_input_summary(value) -> str:
    """One-line summary of a tool input (``command`` / ``path`` / …)."""
    if not isinstance(value, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "description"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            candidate = candidate.strip().replace("\n", " ")
            return f"{key}={candidate[:100]}"
    return ""


def _format_usage_line(usage: dict) -> str:
    """``[tokens] in … · out … · cache hit …% · $…`` for one finished turn."""
    input_tokens = int(usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("output_tokens") or 0)
    cache_read = int(usage.get("cache_read_input_tokens") or 0)
    cache_creation = int(usage.get("cache_creation_input_tokens") or 0)
    billed_input = input_tokens + cache_read + cache_creation
    parts = [f"in {billed_input:,}", f"out {output_tokens:,}"]
    if cache_read or cache_creation:
        hit = cache_read / billed_input * 100 if billed_input else 0.0
        parts.append(f"cache hit {hit:.0f}%")
    cost = usage.get("cost_usd")
    if isinstance(cost, (int, float)):
        parts.append(f"${cost:.4f}")
    model = usage.get("model")
    prefix = f"{model} · " if model else ""
    return f"[usage] {prefix}{' · '.join(parts)}"


async def _stream_and_wait(
    client: ServerClient,
    task_id: str,
    target_daemon_id: str,
    event_printer: _AgentEventPrinter | None = None,
) -> dict | None:
    """Subscribe to the task's SSE stream and render it incrementally.

    ``printed`` is the byte offset already echoed. It advances with each
    delta and re-aligns on every ``snapshot`` / ``done``, so reconnects and
    fallbacks never re-print content the user has already seen (design
    decision 6).

    ``agent_events`` frames are handed to ``event_printer`` (no-op when the
    flags are off or the server is old and never sends them).
    """
    printed = 0

    for attempt in range(2):
        try:
            async for event in client.stream_task(task_id):
                etype = event.get("type")
                if etype == "snapshot":
                    snap = event.get("stdout") or ""
                    tail = snap[printed:]
                    if tail:
                        click.echo(tail, nl=False)
                    printed = len(snap)
                elif etype == "delta":
                    chunk = event.get("chunk") or ""
                    if chunk:
                        click.echo(chunk, nl=False)
                        printed += len(chunk)
                elif etype == "agent_events":
                    if event_printer is not None:
                        for item in event.get("events") or []:
                            event_printer(item)
                elif etype == "done":
                    final = dict(event)
                    final_stdout = final.get("stdout") or ""
                    tail = final_stdout[printed:]
                    if tail:
                        click.echo(tail, nl=False)
                    _finish_stream_line(final_stdout)
                    if event_printer is not None:
                        event_printer.usage_from_done(final)
                    return final
            # Stream ended without a ``done`` frame (server closed early).
            break
        except (httpx.RequestError, RPCRejected):
            # Dead connection / feature-absent server. Reconnect once (the
            # fresh snapshot re-syncs ``printed``); otherwise fall back.
            if attempt == 0:
                continue
            break

    data = await _poll_fallback(client, task_id, target_daemon_id, printed)
    if data is not None and event_printer is not None:
        event_printer.usage_from_done(data)
    return data


async def _poll_fallback(
    client: ServerClient,
    task_id: str,
    target_daemon_id: str,
    printed: int,
) -> dict | None:
    """Legacy wait_task polling, cursor-aware (never re-prints the prefix
    already streamed)."""
    try:
        data = await client.wait_task(task_id)
    except TimeoutError:
        click.echo(
            f"⚠️  No response — is daemon '{target_daemon_id}' online?",
            err=True,
        )
        return None
    stdout = data.get("stdout") or ""
    tail = stdout[printed:]
    if tail:
        click.echo(tail, nl=False)
    _finish_stream_line(stdout)
    return data


def _chat_turn_remote(
    provider: str,
    prompt: str,
    step_id: str,
    target_daemon_id: str,
    server_url: str,
    work_dir_str: str | None,
    identity_did: str | None = None,
    agent_cwd: Path | None = None,
    event_printer: _AgentEventPrinter | None = None,
) -> None:
    """Run one chat turn pinned to a remote daemon via the Server.

    The caller has already created the Step (via Job creation or
    ``/input``); this function only streams/waits for ``step_id`` to
    finish.

    Remote and local-daemon chat are Server-managed: SQLite is the
    authoritative session index. Neither the client cwd nor the daemon
    cwd touches ``sessions.json`` on this path. Use
    ``agt chat --list --daemon=X`` to read it back from the Server.
    """
    async def _remote() -> dict | None:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            return await _stream_and_wait(
                client, step_id, target_daemon_id, event_printer,
            )

    try:
        asyncio.run(_remote())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)


# ── chat ───────────────────────────────────────────────────────────


@cli.command()
@click.option(
    "--provider",
    default=None,
    help="Provider key (e.g. claude, codex, cursor). "
         "Auto-pick first available if omitted "
         "(local probe without --daemon, daemon's runtimes with --daemon). "
         "With --list, filters the session list to this agent.",
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
    help="Create an AGT metadata directory at the given path (bare chat "
         "creates none by default). Does NOT change the agent's working "
         "directory.",
)
@click.option(
    "--no-work-dir",
    is_flag=True,
    help="Use the legacy daemon-local layout (~/.agt/workspaces/<provider>/<id>/) "
         "for both agent cwd and AGT metadata.",
)
@click.option(
    "--identity", "identity_did", default=None,
    help="Identity for each chat turn (Phase 4). Server resolves runtime "
         "via select_runtime and rejects if not bound. Requires --daemon.",
)
@click.option(
    "--list",
    "list_mode",
    is_flag=True,
    help="List chat sessions stored for this cwd (all providers by "
         "default; add --provider to filter) and exit.",
)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume an existing session by id. The agent subprocess is "
         "spawned with --resume <id> (or provider equivalent) so its "
         "history is restored; the same history is also printed to "
         "the terminal before the first prompt.",
)
@click.option(
    "--no-history",
    is_flag=True,
    help="With --resume: skip printing the history; just resume.",
)
@click.option(
    "--dir",
    "agent_cwd_override",
    default=None,
    type=click.Path(path_type=Path),
    help="Agent subprocess cwd on the daemon machine (also where "
         "IDENTITY.md / AGENTS.md are injected). Interpreted on the "
         "daemon, so existence is NOT checked locally — the daemon "
         "creates it if missing. Auto-detected to your cwd when --daemon "
         "points at this machine's local daemon.",
)
@click.option(
    "--file",
    "files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Attach a file to the FIRST chat turn (repeatable, max 10). "
         "With --daemon the file is uploaded to the Server first; local "
         "chat passes the path directly to the agent.",
)
@click.option(
    "--show-tools",
    is_flag=True,
    help="Print tool calls and tool results as one-line annotations "
         "while the turn streams (off by default).",
)
@click.option(
    "--show-thinking",
    is_flag=True,
    help="Print the model's thinking stream while the turn runs "
         "(off by default).",
)
@click.option(
    "--usage",
    "show_usage",
    is_flag=True,
    help="Print a token / cache / cost footer after each turn.",
)
def chat(
    provider: str | None,
    target_daemon_id: str | None,
    server_url: str,
    work_dir: str | None,
    no_work_dir: bool,
    list_mode: bool,
    resume_session_id: str | None,
    no_history: bool,
    identity_did: str | None,
    agent_cwd_override: Path | None,
    files: tuple[Path, ...],
    show_tools: bool,
    show_thinking: bool,
    show_usage: bool,
) -> None:
    if identity_did is not None and target_daemon_id is None:
        click.echo(
            "Error: --identity requires --daemon "
            "(identity runtime resolution happens on the Server).",
            err=True,
        )
        sys.exit(2)
    if len(files) > _MAX_CHAT_ATTACHMENTS:
        click.echo(
            f"Error: at most {_MAX_CHAT_ATTACHMENTS} --file attachments "
            f"per turn (got {len(files)}).",
            err=True,
        )
        sys.exit(2)
    """Interactive chat mode with session continuity.

    With ``--daemon`` each turn is submitted to the Server, pinned to that
    daemon, and polled to completion; the shared ``job_id`` keeps the
    remote agent's session alive across turns. Without it, the local agent
    runs directly.

    If ``--provider`` is omitted, the first available agent is auto-picked.

    The agent's working directory is always the user's cwd at chat start.
    For local direct chat (no ``--daemon``), sessions are stored per cwd
    in ``<cwd>/.agt/sessions.json``; that file supports ``--list``,
    ``--resume`` and multi-turn continuity. For Server-managed chat
    (``--daemon``), SQLite is authoritative and no ``sessions.json`` is
    read or written.
    """
    identity_did = _coerce_identity_did(identity_did)
    cwd = Path(os.getcwd())

    if list_mode:
        list_dir = _chat_list_dir(cwd, target_daemon_id, agent_cwd_override)
        _print_chat_session_list(cwd, provider, target_daemon_id, server_url, list_dir=list_dir)
        return

    if resume_session_id is not None and not no_history:
        _print_resumed_history(cwd, resume_session_id, target_daemon_id, server_url)

    # --resume resolution (remote path). The Server is the authoritative
    # session index for remote daemons — it records every task a daemon
    # executed with its session_id + provider + identity + agent cwd. We
    # resolve once up front so the recovered values can drive provider,
    # identity, the reused job_id, and the agent cwd auto-fill below.
    resume_job_id: str | None = None
    resume_task_id: str | None = None
    resume_dir: str | None = None
    if resume_session_id is not None and target_daemon_id is not None:
        _res = _resolve_remote_session(resume_session_id, target_daemon_id, server_url)
        if _res is None:
            sys.exit(2)
        resume_job_id = _res.get("job_id")
        resume_task_id = _res["task_id"]
        resume_dir = _res.get("dir")
        if provider is None:
            provider = _res["provider"]
        # Identity mismatch is a hard error (same rule as the local path).
        if _res.get("identity_did"):
            if identity_did is None:
                identity_did = _res["identity_did"]
            elif identity_did != _res["identity_did"]:
                click.echo(
                    f"Error: --resume {resume_session_id[:8]}… was started "
                    f"with identity '{_res['identity_did']}'; refusing to "
                    f"resume with '{identity_did}'.",
                    err=True,
                )
                sys.exit(2)

    # Resolve agent_cwd: explicit --dir wins; a remote resume reuses the
    # recorded dir; otherwise auto-detect for the local-daemon case
    # (daemon process alive on this machine AND its id matches --daemon);
    # otherwise refuse — a remote daemon can't see your local cwd.
    agent_cwd: Path | None = None
    if agent_cwd_override is not None:
        agent_cwd = agent_cwd_override
    elif target_daemon_id is not None and resume_dir:
        agent_cwd = Path(resume_dir)
    elif target_daemon_id is not None:
        # 链A: pid-file liveness (same evidence `agt daemon status` uses).
        pid = _local_daemon_alive()
        # 链B: identity from ~/.agt/daemons (only meaningful when alive).
        local_id = _local_daemon_id() if pid is not None else None
        if local_id == target_daemon_id:
            agent_cwd = cwd
        else:
            if pid is None:
                state = "offline"
            elif local_id is None:
                state = "running, but no local entry in ~/.agt/daemons"
            else:
                state = f"running, id={local_id}"
            click.echo(
                f"Error: --daemon={target_daemon_id} is not this machine's "
                f"local daemon (local daemon {state}).\n"
                f"Remote daemons can't see your local cwd. Pass "
                f"`--dir <absolute path the daemon can reach>`,\n"
                f"or start this machine's daemon (`agt daemon start`).",
                err=True,
            )
            sys.exit(2)
    # else: target_daemon_id is None → local-controller path; the
    # local daemon already gets user cwd through its own channels.

    controller = DaemonController() if target_daemon_id is None else None

    # --resume resolution (local path): pull provider + identity from
    # sessions.json in a single lookup (the file is tiny; cost is the file
    # read, not the scan). Both fields were recorded when the session was
    # first created; subsequent updates preserve the originals. The remote
    # path was already handled above via the Server.
    if resume_session_id is not None and target_daemon_id is None:
        _resume_entry = CwdSessionStore.load_or_create(cwd).get(
            resume_session_id
        )
        if _resume_entry is None:
            click.echo(
                f"⚠️  --resume {resume_session_id[:8]}… not found in "
                f"{cwd}/.agt/sessions.json; auto-picking provider and "
                f"no identity.",
                err=True,
            )
        else:
            if provider is None:
                provider = _resume_entry.provider
            # Identity mismatch is a hard error — silently swapping the
            # agent's identity mid-session would break context and is
            # almost always a user mistake. A previously-unrecorded
            # identity (None) accepts the new value as a first-time
            # assignment; a recorded value must match exactly.
            if _resume_entry.identity_did:
                if identity_did is None:
                    identity_did = _resume_entry.identity_did
                elif identity_did != _resume_entry.identity_did:
                    click.echo(
                        f"Error: --resume {resume_session_id[:8]}… was "
                        f"started with identity '{_resume_entry.identity_did}'; "
                        f"refusing to resume with '{identity_did}'.",
                        err=True,
                    )
                    sys.exit(2)

    provider = _pick_provider(
        provider, target_daemon_id, server_url, local_controller=controller,
    )
    # Remote resume reuses the recorded job_id so the Server appends the
    # next turn to the same Job (keeping the SessionModel pointer) and the
    # daemon truly resumes the same session; otherwise start a fresh chat
    # task id for local bookkeeping / banner display.
    job_id = resume_job_id
    task_id = resume_task_id or f"chat-{uuid.uuid4().hex[:8]}"
    # Bare ``agt chat`` no longer creates a per-task meta subdir under
    # ``<cwd>/.agt/tasks/chat-xxx/`` — that directory was always empty
    # (nothing wrote to it) and just accumulated over time. The session
    # index lives at ``<cwd>/.agt/sessions.json`` (keyed by agent_cwd),
    # and the agent subprocess runs in the user's cwd directly. ``--work-dir``
    # and ``--no-work-dir`` overrides still create their dirs as before.
    if work_dir or no_work_dir:
        agt_meta_dir, work_dir_str = _prepare_workdir(
            cwd, task_id, work_dir, no_work_dir,
        )
    else:
        _validate_cwd(cwd)
        _ensure_gitignore(cwd)
        agt_meta_dir = None
        work_dir_str = None

    # Bare ``agt chat`` always starts a NEW session. The user wants
    # this explicit ask in this iteration (no auto-resume of the last
    # session). To continue an existing one, use ``--resume <sid>``.
    initial_resume: str | None = resume_session_id

    mode = f"provider={provider}"
    if target_daemon_id is not None:
        mode += f", daemon={target_daemon_id}"
    if resume_session_id:
        mode += f", resume={resume_session_id[:8]}…"
    if agent_cwd is not None:
        mode += f", dir={agent_cwd}"
    click.echo(
        f"🚀 agt chat mode ({mode}, task={task_id}). "
        f"Type 'exit' or 'quit' to leave.\n"
    )

    _chat_loop(
        controller=controller,
        provider=provider,
        job_id=job_id,
        cwd=cwd,
        agt_meta_dir=agt_meta_dir,
        work_dir_str=work_dir_str,
        target_daemon_id=target_daemon_id,
        server_url=server_url,
        initial_resume=initial_resume,
        identity_did=identity_did,
        agent_cwd=agent_cwd,
        files=files,
        event_printer=_AgentEventPrinter(
            show_tools=show_tools,
            show_thinking=show_thinking,
            show_usage=show_usage,
        ),
    )


# Chat attachments: per-turn cap (the Server enforces the byte limit).
_MAX_CHAT_ATTACHMENTS = 10


def _chat_loop(
    *,
    controller: DaemonController | None,
    provider: str,
    job_id: str | None,
    cwd: Path,
    agt_meta_dir: Path,
    work_dir_str: str | None,
    target_daemon_id: str | None,
    server_url: str,
    initial_resume: str | None,
    identity_did: str | None,
    agent_cwd: Path | None,
    files: tuple[Path, ...] = (),
    event_printer: _AgentEventPrinter | None = None,
) -> None:
    """Read-eval-print loop.

    Local path: each turn spawns a fresh Step (new task_id) and relies on
    the agent's ``--resume session_id`` for continuity.

    Remote path: a Job is created on the first turn (unless resuming) and
    each turn appends a new Step via ``POST /api/jobs/{job_id}/input``.

    ``--file`` attachments belong to the first turn only: the user names
    them on the command line, then types the prompt they describe.
    """
    turn_index = 0
    first_prompt: str | None = None
    current_job_id = job_id
    while True:
        line = _read_chat_prompt()
        if line is None:
            click.echo("\n👋 Bye!")
            return
        if not line:
            continue
        if line in ("exit", "quit", ":q"):
            click.echo("👋 Bye!")
            return

        turn_index += 1
        is_first = turn_index == 1
        if is_first and first_prompt is None:
            first_prompt = line
        current_job_id = _run_chat_turn(
            controller=controller,
            provider=provider,
            line=line,
            job_id=current_job_id,
            cwd=cwd,
            agt_meta_dir=agt_meta_dir,
            work_dir_str=work_dir_str,
            target_daemon_id=target_daemon_id,
            server_url=server_url,
            is_first=is_first,
            initial_resume=initial_resume,
            first_prompt=first_prompt,
            turn_index=turn_index,
            identity_did=identity_did,
            agent_cwd=agent_cwd,
            files=files if is_first else (),
            event_printer=event_printer,
        )


def _read_chat_prompt() -> str | None:
    """Read one prompt line; ``None`` on EOF/interrupt, else trimmed."""
    try:
        return input("agt> ").strip()
    except (EOFError, KeyboardInterrupt):
        return None


def _remote_chat_step_id(
    server_url: str,
    *,
    job_id: str | None,
    prompt: str,
    provider: str,
    target_daemon_id: str | None,
    work_dir_str: str | None,
    identity_did: str | None,
    agent_cwd: Path | None,
    files: tuple[Path, ...] = (),
) -> tuple[str | None, str | None]:
    """Resolve the Step id for one remote chat turn.

    If ``job_id`` is None, creates a new Job with ``prompt`` as the first
    Step. Otherwise appends a new Step via ``POST /api/jobs/{job_id}/input``.

    ``files`` are uploaded to the Server first (deduplicated on content
    hash) and sent as FileParts alongside the TextPart. A failed upload
    aborts the turn with an error — silently dropping an attachment would
    let the agent answer a question it never saw the input for.

    Returns ``(step_id, job_id)`` so the caller can keep the Job id for
    subsequent turns.
    """

    async def _create_or_input() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            message = None
            if files:
                message = await _upload_chat_attachments(client, prompt, files)
            if job_id is None:
                return await client.create_job(
                    prompt=prompt,
                    provider=provider,
                    target_daemon_id=target_daemon_id,
                    work_dir=work_dir_str,
                    identity_did=identity_did,
                    agent_cwd=str(agent_cwd) if agent_cwd is not None else None,
                    message=message,
                )
            return await client.job_input(job_id, prompt, message=message)

    try:
        resp = asyncio.run(_create_or_input())
    except Exception as exc:
        click.echo(f"Error submitting chat turn: {exc}", err=True)
        return None, job_id

    status = resp.get("status")
    if status not in ("queued", "running"):
        click.echo(
            f"Error: job is {status}; cannot append input.",
            err=True,
        )
        return None, job_id
    return resp.get("task_id"), resp.get("job_id", job_id)


async def _upload_chat_attachments(
    client: ServerClient,
    prompt: str,
    files: tuple[Path, ...],
) -> dict:
    """Upload ``files`` and build the A2A Message for one chat turn.

    Each upload is stored by the Server (deduplicated on content hash)
    and referenced by ``agt-upload://<id>``; the daemon resolves the
    bytes at claim time and materializes them under ``.agt/inputs/``.
    """
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for path in files:
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0]
        info = await client.upload_input_file(
            name=path.name,
            content_b64=base64.b64encode(data).decode(),
            mime_type=mime,
            source="cli",
        )
        parts.append({
            "type": "file",
            "uri": info.get("uri") or f"agt-upload://{info['upload_id']}",
            "name": info.get("name") or path.name,
            "mime_type": info.get("mime_type") or mime,
        })
    return {"role": "user", "parts": parts}


def _run_chat_turn(
    *,
    controller: DaemonController | None,
    provider: str,
    line: str,
    job_id: str | None,
    cwd: Path,
    agt_meta_dir: Path,
    work_dir_str: str | None,
    target_daemon_id: str | None,
    server_url: str,
    is_first: bool,
    initial_resume: str | None,
    first_prompt: str | None,
    agent_cwd: Path | None,
    turn_index: int,
    identity_did: str | None,
    files: tuple[Path, ...] = (),
    event_printer: _AgentEventPrinter | None = None,
) -> str | None:
    """Dispatch one chat turn to the local daemon or the remote Server.

    Returns the (possibly new) ``job_id`` for the remote path, or the
    unchanged value for the local path.
    """
    resume_for_turn = initial_resume if is_first else None
    if controller is not None:
        # Local path: each turn is a fresh Step; continuity is via the
        # agent's session resume, not a reused task_id.
        step_id = f"chat-{uuid.uuid4().hex[:8]}"
        _chat_turn_local(
            controller, provider, line, step_id, cwd, agt_meta_dir,
            resume_session_id=resume_for_turn,
            first_prompt=first_prompt if is_first else None,
            turn_index=turn_index,
            files=files,
            event_printer=event_printer,
        )
        return job_id

    # Remote path: create the Job on the first turn, then append Steps.
    step_id, current_job_id = _remote_chat_step_id(
        server_url,
        job_id=job_id,
        prompt=line,
        provider=provider,
        target_daemon_id=target_daemon_id,
        work_dir_str=work_dir_str,
        identity_did=identity_did,
        agent_cwd=agent_cwd,
        files=files,
    )
    if step_id is None:
        return current_job_id
    _chat_turn_remote(
        provider, line, step_id,
        target_daemon_id,  # type: ignore[arg-type]
        server_url, work_dir_str,
        identity_did=identity_did,
        agent_cwd=agent_cwd,
        event_printer=event_printer,
    )
    return current_job_id


def _chat_list_dir(
    cwd: Path,
    target_daemon_id: str | None,
    agent_cwd_override: Path | None,
) -> str | None:
    """Return the daemon-side directory used to filter remote chat lists.

    Local chat lists are already scoped to ``cwd`` by the store, so this
    returns ``None``. For a remote daemon we need the directory on the
    daemon machine: an explicit ``--dir`` wins, otherwise the local daemon
    defaults to the client's cwd. A non-local remote daemon without ``--dir``
    cannot be cwd-filtered, so we error out.
    """
    if target_daemon_id is None:
        return None
    if agent_cwd_override is not None:
        return str(agent_cwd_override)
    pid = _local_daemon_alive()
    local_id = _local_daemon_id() if pid is not None else None
    if local_id == target_daemon_id:
        return str(cwd)
    click.echo(
        "Error: remote chat --list requires --dir <daemon-side cwd> "
        "when --daemon is not this machine's local daemon.",
        err=True,
    )
    sys.exit(2)


def _print_chat_session_list(
    cwd: Path,
    provider: str | None = None,
    target_daemon_id: str | None = None,
    server_url: str = "http://127.0.0.1:8420",
    list_dir: str | None = None,
) -> None:
    """Print a table of chat sessions.

    Local (``target_daemon_id`` None): sessions stored in
    ``<cwd>/.agt/sessions.json``. Remote (``target_daemon_id`` set): sessions
    for the daemon-side directory ``list_dir`` via ``/api/runs?mode=chat``.

    ``provider`` filters to one agent's sessions; ``None`` lists all.
    """
    if target_daemon_id is not None:
        _print_remote_session_list(target_daemon_id, provider, server_url, list_dir=list_dir)
        return

    store = CwdSessionStore.load_or_create(cwd)
    entries = store.list_sessions()
    if provider is not None:
        entries = [e for e in entries if e.provider == provider]
    if not entries:
        if provider is not None:
            click.echo(f"No {provider} chat sessions in {cwd}/.agt/ yet.")
        else:
            click.echo(f"No chat sessions in {cwd}/.agt/ yet.")
        return
    click.echo(f"{len(entries)} session(s) in {cwd}/.agt/\n")
    now = time.time()
    rows = []
    for e in entries:
        age_s = max(0, now - e.last_seen_at)
        rows.append((
            e.session_id,
            e.provider,
            _humanize_age(age_s),
            str(e.turn_count),
            _truncate(e.first_prompt, 60),
        ))
    widths = [
        max(len(r[0]) for r in rows),
        max(len(r[1]) for r in rows),
        max(len(r[2]) for r in rows),
        max(len(r[3]) for r in rows),
        0,
    ]
    widths[4] = max(len(r[4]) for r in rows)
    header = (
        f"  {'SESSION':<{widths[0]}}  {'PROVIDER':<{widths[1]}}  "
        f"{'LAST USED':<{widths[2]}}  {'TURNS':<{widths[3]}}  FIRST PROMPT"
    )
    click.echo(header)
    click.echo("  " + "─" * (sum(widths) + 10))
    for r in rows:
        click.echo(
            f"  {r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  "
            f"{r[2]:<{widths[2]}}  {r[3]:<{widths[3]}}  {r[4]}"
        )


def _print_remote_session_list(
    daemon_id: str,
    provider: str | None,
    server_url: str,
    list_dir: str | None = None,
) -> None:
    """Print chat sessions for a daemon, filtered by daemon-side cwd."""

    async def _run() -> dict:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            return await client.list_runs(
                mode="chat",
                cwd=list_dir,
                daemon_id=daemon_id,
                provider=provider,
                limit=200,
            )

    try:
        result = asyncio.run(_run())
    except Exception as exc:
        click.echo(
            f"Error listing sessions for daemon '{daemon_id}': {exc}", err=True,
        )
        return

    sessions = result.get("runs", [])
    if not sessions:
        label = f"{provider} " if provider else ""
        dir_label = f" in {list_dir}" if list_dir else ""
        click.echo(f"No {label}chat sessions{dir_label} on daemon '{daemon_id}' yet.")
        return
    click.echo(f"{len(sessions)} session(s) on daemon '{daemon_id}'{f' in {list_dir}' if list_dir else ''}\n")
    rows = [
        (
            s.get("session_id", ""),
            s.get("provider", ""),
            s.get("cwd") or s.get("dir") or "—",
            s.get("updated_at", ""),
            _truncate(s.get("title") or s.get("first_prompt", ""), 60),
        )
        for s in sessions
    ]
    widths = [
        max(len(r[0]) for r in rows),
        max(len(r[1]) for r in rows),
        max(len(r[2]) for r in rows),
        max(len(r[3]) for r in rows),
        0,
    ]
    widths[4] = max(len(r[4]) for r in rows)
    header = (
        f"  {'SESSION':<{widths[0]}}  {'PROVIDER':<{widths[1]}}  "
        f"{'DIR':<{widths[2]}}  {'UPDATED':<{widths[3]}}  FIRST PROMPT"
    )
    click.echo(header)
    click.echo("  " + "─" * (sum(widths) + 10))
    for r in rows:
        click.echo(
            f"  {r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  "
            f"{r[2]:<{widths[2]}}  {r[3]:<{widths[3]}}  {r[4]}"
        )


def _resolve_remote_session(
    session_id: str,
    target_daemon_id: str,
    server_url: str,
) -> dict | None:
    """Resolve a remote session via the Server for ``--resume``.

    Returns the resolve record (task_id / provider / identity_did / dir /
    daemon_id) on success, or ``None`` after printing an error (the caller
    exits). Validates that the session actually belongs to the requested
    daemon so a session from another daemon can't be resumed here.
    """

    async def _run() -> dict | None:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            return await client.resolve_session(session_id)

    try:
        res = asyncio.run(_run())
    except Exception as exc:
        click.echo(
            f"Error: failed to resolve session via Server ({server_url}): {exc}",
            err=True,
        )
        return None
    if res is None:
        click.echo(
            f"Error: session {session_id[:8]}… not found on Server "
            f"{server_url}; remote sessions are not stored locally.",
            err=True,
        )
        return None
    if res.get("daemon_id") and res["daemon_id"] != target_daemon_id:
        click.echo(
            f"Error: session {session_id[:8]}… belongs to daemon "
            f"'{res['daemon_id']}', not '{target_daemon_id}'.",
            err=True,
        )
        return None
    return res


def _print_resumed_history(
    cwd: Path,
    session_id: str,
    target_daemon_id: str | None,
    server_url: str,
) -> None:
    """Print a session's past transcript before resuming it.

    Local resume reads the transcript straight off this machine; remote
    resume (``--daemon``) asks the Server to fetch it from the owning
    daemon, where the agent's ``~/.claude`` / ``~/.codex`` actually live.
    """
    if target_daemon_id is not None:
        _print_remote_resumed_history(session_id, server_url)
        return
    store = CwdSessionStore.load_or_create(cwd)
    entry = store.get(session_id)
    if entry is None:
        click.echo(
            f"⚠️  session {session_id} not found in {cwd}/.agt/sessions.json",
            err=True,
        )
        sys.exit(2)
    try:
        history = load_history(entry.provider, session_id)
    except NotImplementedError as exc:
        click.echo(f"⚠️  {exc}", err=True)
        history = []
    _render_history(session_id, history)


def _print_remote_resumed_history(session_id: str, server_url: str) -> None:
    """Fetch and print a remote session's transcript via the Server.

    Best-effort: history display never blocks the resume. A missing session,
    an offline daemon, or an unimplemented provider reader each produce a
    one-line hint and the resume proceeds.
    """

    async def _run() -> dict | None:
        async with ServerClient(base_url=server_url, daemon_id="cli-direct") as client:
            return await client.get_session_history(session_id)

    try:
        result = asyncio.run(_run())
    except RPCRejected as exc:
        if exc.status == 503:
            click.echo(
                "⚠️  daemon offline — remote history unavailable right now.",
                err=True,
            )
        else:
            click.echo(f"⚠️  could not fetch remote history: {exc}", err=True)
        return
    except Exception as exc:
        click.echo(f"⚠️  could not fetch remote history: {exc}", err=True)
        return
    if result is None:
        click.echo(
            f"⚠️  session {session_id[:8]}… not found on Server; no history to show.",
            err=True,
        )
        return
    if result.get("not_implemented"):
        click.echo(
            "⚠️  history reader for this provider is not yet implemented.",
            err=True,
        )
        return
    history = [
        Message(role=m.get("role", ""), content=m.get("content", ""),
                timestamp=m.get("timestamp"))
        for m in result.get("messages", [])
    ]
    _render_history(session_id, history)


def _render_history(session_id: str, history: list[Message]) -> None:
    """Print a transcript (shared by the local and remote resume paths)."""
    if not history:
        click.echo(
            f"(no history available for {session_id[:8]}…)",
            err=True,
        )
        return
    click.echo(f"─── history of {session_id[:8]}… ({len(history)} message(s)) ───")
    for msg in history:
        marker = ">" if msg.role == "user" else "·"
        click.echo(f"{marker} {msg.role}: {msg.content}")
    click.echo("─── end history ───\n")


def _humanize_age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)} min ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)} hours ago"
    return f"{int(seconds // 86400)} days ago"


def _truncate(text: str, limit: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── session management (server-side chat sessions) ────────────────


@cli.group()
def session() -> None:
    """Manage chat sessions recorded by the Server."""


@session.command("delete")
@click.argument("session_ids", nargs=-1, required=True)
@click.option(
    "--daemon",
    "daemon_id",
    required=True,
    help="Daemon that owns every session being deleted.",
)
@click.option(
    "--provider",
    required=True,
    help="Provider that owns every session being deleted.",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the permanent-deletion confirmation prompt.",
)
@click.option(
    "--finalize-active",
    is_flag=True,
    help="Cancel a waiting_input job before deleting its session.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def session_delete(
    session_ids: tuple[str, ...],
    daemon_id: str,
    provider: str,
    assume_yes: bool,
    finalize_active: bool,
    server_url: str,
) -> None:
    """Permanently delete one or more provider chat sessions.

    Removes the AGT session pointers, the daemon-side cwd index entry, and
    the provider transcript. SESSION_IDS must all belong to the same
    DAEMON and PROVIDER.
    """
    # Preserve order while dropping duplicates from shell completion or
    # repeated paste; deleting the same transcript twice is harmless but noisy.
    unique_ids = list(dict.fromkeys(session_ids))
    if not assume_yes:
        sample = ", ".join(sid[:8] for sid in unique_ids[:3])
        more = f" … (+{len(unique_ids) - 3})" if len(unique_ids) > 3 else ""
        click.confirm(
            f"Permanently delete {len(unique_ids)} {provider} session(s) "
            f"on daemon '{daemon_id}' ({sample}{more})?",
            abort=True,
        )

    async def _run() -> tuple[list[str], list[tuple[str, str]]]:
        deleted: list[str] = []
        failed: list[tuple[str, str]] = []
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            for session_id in unique_ids:
                try:
                    await client.delete_session(
                        session_id,
                        provider=provider,
                        daemon_id=daemon_id,
                        finalize_active=finalize_active,
                    )
                    deleted.append(session_id)
                except Exception as exc:
                    failed.append((session_id, str(exc)))
        return deleted, failed

    try:
        deleted, failed = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    for session_id in deleted:
        click.echo(f"🗑️  {session_id}: deleted")
    for session_id, reason in failed:
        click.echo(f"⚠️  {session_id}: {reason}", err=True)
    click.echo(f"{len(deleted)} deleted, {len(failed)} failed.")
    if failed:
        sys.exit(1)


# ── server (Phase 2) ──────────────────────────────────────────────


@cli.group()
def server() -> None:
    """Server-related subcommands."""


@server.command("init")
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Regenerate the secret even if one already exists.",
)
def server_init(force: bool) -> None:
    """Create the initial server secret in ~/.agt/server.secret.

    This is the fastest way to get a local Server running: it generates a
    secure random secret, persists it, and makes it auto-discoverable by
    both ``agt server start`` and ``agt daemon start`` without exporting
    an environment variable every time. Run it once per machine (or once
    per ``~/.agt`` directory).
    """
    import secrets

    agt_dir = Path.home() / ".agt"
    agt_dir.mkdir(parents=True, exist_ok=True)
    secret_path = agt_dir / "server.secret"

    if secret_path.exists() and not force:
        click.echo(
            f"Server secret already exists at {secret_path}; "
            "use --force to regenerate."
        )
    else:
        secret = secrets.token_hex(32)
        secret_path.write_text(secret, encoding="utf-8")
        secret_path.chmod(0o600)
        click.echo(f"Generated server secret at {secret_path}")

    click.echo(
        "\nThe secret is now auto-loaded by `agt server start` and "
        "`agt daemon start`. "
        "If you prefer environment variables, run:"
    )
    click.echo(
        f'    export AGT_SERVER_SECRET="$(cat {secret_path})"'
    )


def _stop_if_running(pid: int, label: str, timeout: float = 10.0) -> None:
    """SIGTERM a running server/daemon and wait for it to actually exit.

    Used by ``server start --detach`` / ``daemon start --detach`` so a
    stale process (e.g. one still serving an older build) is replaced
    rather than keeping the port / pidfile held by the old instance.
    Waits on ``os.kill(pid, 0)`` raising ProcessLookupError — that is the
    moment the process is truly gone (and any listening socket closed),
    which is later than the pidfile unlink the old process does on its
    own shutdown path.
    """
    click.echo(f"Stopping existing {label} (pid {pid})…")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        click.echo(f"{label} already gone.")
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            click.echo(f"Stopped {label} (pid {pid}).")
            return
        time.sleep(0.2)
    click.echo(
        f"⚠️  {label} (pid {pid}) did not exit within {timeout:.0f}s; "
        "proceeding anyway.",
        err=True,
    )


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
@click.option(
    "-d", "--detach", is_flag=True,
    help="Run the server in the background; this command returns immediately. "
         "Logs go to ~/.agt/server.log (or --log-file). "
         "Use 'agt server stop' to terminate.",
)
@click.option(
    "--log-file", default=None,
    help="Log file path (default ~/.agt/server.log). Only used when --detach is set.",
)
@click.option(
    "--pid-file", default=None,
    help="PID file path (default ~/.agt/server.pid). Written in --detach mode; "
         "also read at startup to detect and replace a running server.",
)
def server_start(
    host: str,
    port: int,
    db_path: str | None,
    detach: bool,
    log_file: str | None,
    pid_file: str | None,
) -> None:
    """Start the FastAPI Server.

    Foreground (default): parent blocks; Ctrl-C to stop.
    ``--detach``: fork a child running uvicorn; parent exits immediately.
    """
    # Validate configuration before importing the server module or spawning
    # a detached child. On a first run we generate the secret here rather
    # than failing: `agt server start` is the first command most users run,
    # and the value is auto-discovered by both the Server and any local
    # daemon, so there is nothing to export.
    from ..server.config import ServerConfig, ServerConfigError
    from ..server_secret import (
        default_server_secret_path,
        ensure_server_secret,
    )

    secret_path = default_server_secret_path()
    had_secret = bool(secret_path.exists()) or bool(
        os.environ.get("AGT_SERVER_SECRET", "").strip()
    )
    try:
        created_secret = ensure_server_secret()
    except OSError as exc:
        click.echo(f"Error: cannot create {secret_path}: {exc}", err=True)
        sys.exit(1)
    if not had_secret and created_secret:
        click.echo(f"Generated server secret at {secret_path}")

    try:
        ServerConfig.from_env()
    except ServerConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    # Replace any already-running server first (both modes). Foreground
    # would otherwise fail to bind the port ("address already in use");
    # detached would leave a stale process still serving older code.
    pid_path = Path(pid_file) if pid_file else Path.home() / ".agt" / "server.pid"
    from ..server.pidfile import _read_pidfile as _server_read_pidfile
    existing = _server_read_pidfile(pid_path)
    if existing is not None:
        _stop_if_running(existing, "server")

    if not detach:
        import argparse

        from ..server.main import cli_main

        args = argparse.Namespace(
            host=host,
            port=port,
            db_path=db_path,
            sessions_json=None,
            detach=False,
            log_file=None,
            pid_file=None,
        )
        sys.exit(cli_main(args))

    # Detached: Popen a child running ``agt.server.main`` (same entry
    # uvicorn uses when ``uvicorn agt.server.main:app`` is invoked).
    log_path = Path(log_file) if log_file else Path.home() / ".agt" / "server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "ab", buffering=0)

    env = os.environ.copy()
    # Record the exact pid-file path so ``agt server stop`` / ``status``
    # (called from a different shell) can find it, even when the user
    # passed an explicit --pid-file that differs from the default.
    env["AGT_SERVER_PID_FILE"] = str(pid_path)

    cmd = [sys.executable, "-m", "agt.server.main",
           "--host", host, "--port", str(port),
           "--log-file", str(log_path),
           "--pid-file", str(pid_path),
           "--detach"]
    if db_path:
        cmd += ["--db-path", db_path]

    child = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        start_new_session=True,
        close_fds=True,
        env=env,
    )
    click.echo(
        f"Server started in background (pid={child.pid}, log={log_path})"
    )
    sys.exit(0)


@server.command("stop")
@click.option(
    "--pid-file", default=None,
    help="PID file path. Defaults to AGT_SERVER_PID_FILE if set, "
         "else ~/.agt/server.pid.",
)
def server_stop(pid_file: str | None) -> None:
    """Signal the running server to terminate (SIGTERM)."""
    from ..server.pidfile import _signal_pid

    path = _resolve_server_pid_file(pid_file)
    pid = _read_pidfile_live(path)
    if pid is None:
        click.echo("No server running (pidfile absent or stale).")
        return
    try:
        _signal_pid(pid)
    except ProcessLookupError:
        click.echo(f"Server pid {pid} not found.", err=True)
        sys.exit(1)
    click.echo(f"Sent SIGTERM to server pid {pid}")


@server.command("status")
@click.option(
    "--pid-file", default=None,
    help="PID file path. Defaults to AGT_SERVER_PID_FILE if set, "
         "else ~/.agt/server.pid.",
)
def server_status(pid_file: str | None) -> None:
    """Show whether the server is currently running."""
    path = _resolve_server_pid_file(pid_file)
    pid = _read_pidfile_live(path)
    if pid is None:
        click.echo("Server is NOT running.")
        sys.exit(1)
    click.echo(f"Server is running, pid={pid}, pidfile={path}")
    port = int(os.environ.get("AGT_SERVER_PORT", "8420"))
    click.echo(f"Console: http://127.0.0.1:{port}/")


def _resolve_server_pid_file(explicit: str | None) -> Path:
    """Pick the pid-file path: explicit flag wins, else AGT_SERVER_PID_FILE
    env (set by ``agt server start --detach``), else ``~/.agt/server.pid``."""
    if explicit:
        return Path(explicit)
    env_path = os.environ.get("AGT_SERVER_PID_FILE")
    if env_path:
        return Path(env_path)
    return Path.home() / ".agt" / "server.pid"


def _read_pidfile_live(path) -> int | None:
    """Thin wrapper around ``server.pidfile._read_pidfile`` for CLI callers."""
    from ..server.pidfile import _read_pidfile

    return _read_pidfile(path)


def _pid_is_agt_daemon(pid: int) -> bool:
    """Best-effort guard against pid reuse: the process behind ``pid``
    should look like an agt daemon (``/proc/<pid>/cmdline`` mentions
    both agt and daemon). Non-Linux / unreadable proc → True (trust the
    ``os.kill`` liveness probe alone).
    """
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_text(errors="replace")
        return "agt" in cmdline and "daemon" in cmdline
    except OSError:
        return True


def _local_daemon_alive() -> int | None:
    """Return the local daemon's pid if it is alive, else None.

    Same evidence ``agt daemon status`` uses: ``~/.agt/daemon.pid``
    (written unconditionally by the daemon on startup) plus
    ``os.kill(pid, 0)`` liveness and a cmdline sanity check.
    """
    pid_path = Path.home() / ".agt" / "daemon.pid"
    if not pid_path.exists():
        return None
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    if not _pid_is_agt_daemon(pid):
        return None
    return pid


def _local_daemon_id() -> str | None:
    """Return the daemon id this machine's daemon registers under.

    Mirrors ``DaemonConfig.from_env``'s derivation so CLI and daemon
    agree without any side-channel file: ``AGT_DAEMON_ID`` env wins;
    otherwise find the self entry in ``~/.agt/daemons`` (port match +
    local host, see ``_find_self_daemon``) and derive
    ``uuid5(AGT_DAEMON_NAMESPACE, url)``. None when no self entry
    exists (daemon not started via ``agt daemon start``, or the
    daemons file lacks a local entry).
    """
    env = os.environ.get("AGT_DAEMON_ID")
    if env:
        return env
    from ..daemon.config import _find_self_daemon
    from ..ids import daemon_id_from_url

    ws_port = int(os.environ.get("AGT_DAEMON_WS_PORT", "8422"))
    entry = _find_self_daemon(ws_port)
    if entry is None:
        return None
    return daemon_id_from_url(entry[1])


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


@task.command("list")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    help="Filter by task status (pending/running/completed/failed/timeout/cancelled). "
         "Repeat for multiple values. Omit for all statuses.",
)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    help="Max number of tasks to return (1-1000).",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def task_list(statuses: tuple[str, ...], limit: int, server_url: str) -> None:
    """List tasks on the Server (newest first)."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_tasks(
                statuses=list(statuses) if statuses else None,
                limit=limit,
            )

    try:
        tasks = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not tasks:
        click.echo("No tasks on the Server.")
        return

    click.echo(f"📋 {len(tasks)} task(s) on the Server\n")
    click.echo(
        f"  {'TASK_ID':<10} {'PROVIDER':<10} {'STATUS':<10} "
        f"{'EXIT':<6} {'DURATION':<9} {'UPDATED':<19}"
    )
    click.echo("  " + "─" * 70)
    for t in tasks:
        exit_code = t.get("exit_code")
        exit_str = str(exit_code) if exit_code is not None else "—"
        duration = t.get("duration")
        duration_str = f"{duration:.2f}s" if duration is not None else "—"
        click.echo(
            f"  {t['task_id']:<10} {t['provider']:<10} {t['status']:<10} "
            f"{exit_str:<6} {duration_str:<9} {t.get('updated_at', '') or '—':<19}"
        )


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
        f"  {'daemon_id':<38} {'name':<14} {'provider':<12} {'version':<12} "
        f"{'device':<24} {'status':<8}"
    )
    click.echo("  " + "─" * 108)
    for r in runtimes:
        name = r.get("daemon_name") or "—"
        click.echo(
            f"  {r['daemon_id']:<38} {name[:14]:<14} {r['type']:<12} "
            f"{r['version']:<12} {r['device_name']:<24} {r['status']:<8}"
        )


# ── identity (Phase 4 Step 1) ────────────────────────────────────────


@cli.group()
def identity() -> None:
    """Identity management (Phase 4): create and manage agent identities."""


@identity.command("list")
@click.option("--daemon", "daemon_id", default=None, help="Filter by daemon_id.")
@click.option("--status", "status_filter", default=None,
              help="Filter by status (active/deprecated/revoked).")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_list(daemon_id: str | None, status_filter: str | None, server_url: str) -> None:
    """List identities on the Server."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_identities(
                daemon_id=daemon_id,
                status=status_filter,
            )

    try:
        identities = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not identities:
        click.echo("No identities on the Server.")
        return

    click.echo(f"🪪 {len(identities)} identit(y/ies) on the Server\n")
    click.echo(
        f"  {'DID':<48} {'NAME':<20} {'STATUS':<12} {'CAPABILITIES':<24}"
    )
    click.echo("  " + "─" * 108)
    for ident in identities:
        caps = ",".join(ident.get("capabilities", [])) or "—"
        click.echo(
            f"  {ident['did']:<48} {ident['name'][:20]:<20} "
            f"{ident['status']:<12} {caps[:24]:<24}"
        )


@identity.command("show")
@click.argument("did")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_show(did: str, server_url: str) -> None:
    """Show one identity's full details."""

    async def _run() -> dict | None:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.get_identity(did)

    try:
        ident = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if ident is None:
        click.echo(f"Identity not found: {did}", err=True)
        sys.exit(2)

    caps = ", ".join(ident.get("capabilities", [])) or "—"
    click.echo(f"did         : {ident['did']}")
    click.echo(f"daemon_id   : {ident['daemon_id']}")
    click.echo(f"name        : {ident['name']}")
    click.echo(f"description : {ident.get('description') or '—'}")
    click.echo(f"capabilities: {caps}")
    click.echo(f"status      : {ident['status']}")
    click.echo(f"visibility  : {ident.get('visibility', 'private')}")
    click.echo(f"created_by  : {ident.get('created_by') or '—'}")
    click.echo(f"created_at  : {ident.get('created_at', '—')}")
    click.echo(f"updated_at  : {ident.get('updated_at', '—')}")


@identity.command("create")
@click.option("--name", required=True, help="Human-readable identity name.")
@click.option("--slug", default=None,
              help="Machine slug (default: derived from name).")
@click.option("--daemon-id", default=None,
              help="Owning daemon namespace (default: 'local').")
@click.option("--description", default="", help="Identity description.")
@click.option("--capability", "capabilities", multiple=True,
              help="Capability tag; repeat for multiple.")
@click.option("--visibility", default="private",
              type=click.Choice(["private", "public"]),
              help="Cross-daemon visibility (default: private).")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_create(
    name: str,
    slug: str | None,
    daemon_id: str | None,
    description: str,
    capabilities: tuple[str, ...],
    visibility: str,
    server_url: str,
) -> None:
    """Create a new identity (also writes AGENTS.md to ~/.agt/identities/)."""

    payload = {
        "daemon_id": daemon_id or "local",
        "name": name,
        "slug": slug,
        "description": description,
        "capabilities": list(capabilities),
        "visibility": visibility,
    }

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.create_identity(payload)

    try:
        ident = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"✅ Created identity: {ident['did']}")
    click.echo(f"  AGENTS.md: ~/.agt/identities/<slug>/AGENTS.md (opt-in consumption)")


@identity.command("update")
@click.argument("did")
@click.option("--name", default=None, help="New name.")
@click.option("--description", default=None, help="New description.")
@click.option("--add-capability", "add_capabilities", multiple=True,
              help="Capability to add; repeat for multiple.")
@click.option("--remove-capability", "remove_capabilities", multiple=True,
              help="Capability to remove; repeat for multiple.")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_update(
    did: str,
    name: str | None,
    description: str | None,
    add_capabilities: tuple[str, ...],
    remove_capabilities: tuple[str, ...],
    server_url: str,
) -> None:
    """Update an identity (AGENTS.md is NOT overwritten; use sync-files)."""

    payload: dict = {}
    if name is not None:
        payload["name"] = name
    if description is not None:
        payload["description"] = description
    if add_capabilities:
        payload["add_capabilities"] = list(add_capabilities)
    if remove_capabilities:
        payload["remove_capabilities"] = list(remove_capabilities)

    if not payload:
        click.echo("Nothing to update. Provide at least one of --name, "
                   "--description, --add-capability, --remove-capability.", err=True)
        sys.exit(2)

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.update_identity(did, payload)

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"✅ Updated identity: {did}")
    click.echo("  (AGENTS.md unchanged; run `agt identity sync-files "
               f"{did}` to regenerate)")


@identity.command("deprecate")
@click.argument("did")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_deprecate(did: str, server_url: str) -> None:
    """Soft-delete an identity (status='deprecated')."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.deprecate_identity(did)

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"🗑️  Deprecated identity: {did}")


@identity.command("revoke")
@click.argument("did")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_revoke(did: str, server_url: str) -> None:
    """Hard-revoke an identity (status='revoked')."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.revoke_identity(did)

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"⛔ Revoked identity: {did}")


@identity.command("delete")
@click.argument("did")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_delete(did: str, server_url: str) -> None:
    """Physically delete an identity (fails if tasks reference it)."""

    async def _run() -> bool:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.delete_identity(did)

    try:
        deleted = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if deleted:
        click.echo(f"❌ Deleted identity: {did}")
    else:
        click.echo(f"Identity not found: {did}", err=True)
        sys.exit(2)


@identity.command("sync-files")
@click.argument("did")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_sync_files(did: str, server_url: str) -> None:
    """Force-regenerate the AGENTS.md file from database state."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.sync_identity_files(did)

    try:
        asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(f"🔄 Synced AGENTS.md for identity: {did}")
    click.echo("  (overwrites manual edits with database truth)")


# ── identity bindings + selection (Phase 4 Step 2) ──────────────────


def _find_binding_id(bindings: list[dict], daemon_id: str, provider: str) -> int | None:
    """Locate a binding id by (daemon, provider) triple; None if absent."""
    for b in bindings:
        if b.get("daemon_id") == daemon_id and b.get("provider") == provider:
            return int(b["id"])
    return None


@identity.command("bind")
@click.argument("did")
@click.option("--daemon", "daemon_id", required=True, help="Daemon namespace.")
@click.option("--runtime", "provider", required=True, help="Provider to bind.")
@click.option("--priority", default=0, show_default=True,
              help="Preference weight (higher wins when no default).")
@click.option("--default", "is_default", is_flag=True,
              help="Mark as the identity's default (atomically unflags others).")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_bind(
    did: str,
    daemon_id: str,
    provider: str,
    priority: int,
    is_default: bool,
    server_url: str,
) -> None:
    """Bind an identity to a runtime (daemon + provider)."""

    payload = {
        "daemon_id": daemon_id,
        "provider": provider,
        "priority": priority,
        "is_default": is_default,
    }

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.create_binding(did, payload)

    try:
        binding = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    marker = " (default)" if binding.get("is_default") else ""
    click.echo(
        f"🔗 Bound {did} → {binding['daemon_id']}/{binding['provider']}{marker}"
    )


@identity.command("unbind")
@click.argument("did")
@click.option("--daemon", "daemon_id", required=True, help="Daemon namespace.")
@click.option("--runtime", "provider", required=True, help="Provider to unbind.")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_unbind(did: str, daemon_id: str, provider: str, server_url: str) -> None:
    """Remove a binding (located by daemon + runtime)."""

    async def _run() -> bool:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            bindings = await client.list_bindings(did, daemon_id=daemon_id)
            binding_id = _find_binding_id(bindings, daemon_id, provider)
            if binding_id is None:
                return False
            return await client.delete_binding(did, binding_id)

    try:
        deleted = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if deleted:
        click.echo(f"🔓 Unbound {daemon_id}/{provider} from {did}")
    else:
        click.echo(
            f"No binding for {did} on daemon '{daemon_id}' provider '{provider}'",
            err=True,
        )
        sys.exit(2)


@identity.command("bindings")
@click.argument("did")
@click.option("--daemon", "daemon_id", default=None, help="Filter by daemon_id.")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_bindings(did: str, daemon_id: str | None, server_url: str) -> None:
    """List an identity's bindings."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_bindings(did, daemon_id=daemon_id)

    try:
        bindings = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not bindings:
        click.echo(f"No bindings for {did}. Use 'agt identity bind' to add one.")
        return

    click.echo(f"🔗 {len(bindings)} binding(s) for {did}\n")
    click.echo(
        f"  {'DAEMON':<16} {'RUNTIME':<12} {'PRIORITY':<10} {'DEFAULT':<8}"
    )
    click.echo("  " + "─" * 50)
    for b in bindings:
        click.echo(
            f"  {b['daemon_id'][:16]:<16} {b['provider'][:12]:<12} "
            f"{b['priority']:<10} {'yes' if b['is_default'] else '—':<8}"
        )


@identity.command("default")
@click.argument("did")
@click.option("--daemon", "daemon_id", required=True, help="Daemon namespace.")
@click.option("--runtime", "provider", required=True, help="Provider to make default.")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def identity_default(did: str, daemon_id: str, provider: str, server_url: str) -> None:
    """Switch an identity's default binding (atomic)."""

    async def _run() -> dict:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            bindings = await client.list_bindings(did, daemon_id=daemon_id)
            binding_id = _find_binding_id(bindings, daemon_id, provider)
            if binding_id is None:
                return {}
            return await client.set_default_binding(did, binding_id)

    try:
        binding = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not binding:
        click.echo(
            f"No binding for {did} on daemon '{daemon_id}' provider '{provider}'",
            err=True,
        )
        sys.exit(2)

    click.echo(
        f"⭐ Default for {did} → {binding['daemon_id']}/{binding['provider']}"
    )


# ── daemon (Phase 2 real implementation) ────────────────────────────


@cli.group()
def daemon() -> None:
    """Daemon-related subcommands."""


@daemon.command("start")
@click.option("--server-url", default=None, help="Override AGT_SERVER_URL.")
@click.option(
    "-d", "--detach", is_flag=True,
    help="Run the daemon in the background; this command returns immediately. "
         "Logs go to ~/.agt/daemon.log (or --log-file). "
         "Use 'agt daemon stop' to terminate.",
)
@click.option(
    "--log-file", default=None,
    help="Log file path (default ~/.agt/daemon.log). "
         "Only used when --detach is set.",
)
def daemon_start(
    server_url: str | None,
    detach: bool,
    log_file: str | None,
) -> None:
    """Start the background daemon (connects to Server, registers runtimes)."""
    # Replace any already-running daemon first (both modes) — otherwise the
    # child's pidfile guard (``run_background``) refuses to start.
    from ..daemon.main import _read_pidfile as _daemon_read_pidfile
    existing = _daemon_read_pidfile()
    if existing is not None:
        _stop_if_running(existing, "daemon")

    if not detach:
        # Foreground: parent blocks until Ctrl-C, as before.
        import argparse
        from ..daemon.main import cmd_start

        args = argparse.Namespace(server_url=server_url)
        sys.exit(cmd_start(args))

    # Detached: spawn a child running the existing cmd_start, parent exits.
    log_path = Path(log_file) if log_file else Path.home() / ".agt" / "daemon.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_fp = open(log_path, "ab", buffering=0)

    env = os.environ.copy()
    # ``DaemonConfig.server_url`` is a read-only property driven by the
    # ``AGT_SERVER_URL`` env var (the CLI's own ``--server-url`` argument
    # to ``cmd_start`` is a long-broken attribute write — see issue
    # outside this change's scope). Forward via env, which is the path
    # ``from_env()`` actually reads.
    if server_url:
        env["AGT_SERVER_URL"] = server_url

    child = subprocess.Popen(
        [sys.executable, "-m", "agt.daemon.main", "start"],
        stdin=subprocess.DEVNULL,
        stdout=log_fp,
        stderr=log_fp,
        start_new_session=True,  # detach from parent's tty/process group
        close_fds=True,
        env=env,
    )
    click.echo(
        f"Daemon started in background (pid={child.pid}, log={log_path})"
    )
    sys.exit(0)


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


def _ws_port_from_url(url: str, fallback: int = 8422) -> int:
    """Best-effort port extraction for the invite payload."""
    from ..daemon.config import _port_of_url

    raw = _port_of_url(url)
    try:
        return int(raw) if raw else fallback
    except ValueError:
        return fallback


@daemon.command("invite")
@click.option("--name", default=None, help="Daemon label (e.g. cloud-01).")
@click.option("--url", "url", default=None, help="Daemon's public ws:// URL.")
@click.option(
    "--existing",
    default=None,
    help="Re-use an entry already present in ~/.agt/daemons (by name).",
)
@click.option(
    "--daemon-id",
    "daemon_id",
    default=None,
    help="Explicit daemon id. Defaults to the existing entry's id or a new uuid4.",
)
@click.option(
    "--add",
    "add_entry",
    is_flag=True,
    help="Register the target in this machine's ~/.agt/daemons.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Allow replacing a name that is already registered with another URL.",
)
@click.option(
    "--server-url",
    default=None,
    help="Server base URL to embed (only needed by HTTP-mode daemons).",
)
def daemon_invite(
    name: str | None,
    url: str | None,
    existing: str | None,
    daemon_id: str | None,
    add_entry: bool,
    force: bool,
    server_url: str | None,
) -> None:
    """Generate a one-paste invite code for a remote daemon.

    The code carries the server secret, the daemon's public URL, its
    daemon id and listen port. Run the printed ``agt daemon init``
    command on the daemon machine; nothing else needs to be copied.

    Treat the code like an SSH private key: anyone holding it can
    connect to this Server as that daemon.
    """
    from ..daemons_file import (
        DaemonEntry,
        DaemonsFileError,
        daemons_file_path,
        read_daemons_entries,
        upsert_daemons_entry,
    )
    from ..invite import InviteError, encode_invite
    from ..server_secret import load_server_secret

    secret = load_server_secret()
    if not secret:
        click.echo(
            "Error: no server secret configured. Start the server once "
            "(`agt server start`) or run `agt server init`.",
            err=True,
        )
        sys.exit(1)

    if existing:
        match = next(
            (e for e in read_daemons_entries() if e.name == existing), None
        )
        if match is None:
            click.echo(
                f"Error: no entry named '{existing}' in {daemons_file_path()}",
                err=True,
            )
            sys.exit(1)
        name = match.name
        url = url or match.url
        daemon_id = daemon_id or match.daemon_id

    if not name:
        click.echo("Error: --name is required.", err=True)
        sys.exit(2)
    if not url:
        click.echo("Error: --url is required (or use --existing).", err=True)
        sys.exit(2)

    try:
        from ..daemons_file import validate_daemon_id, validate_name, validate_url

        validate_name(name)
        validate_url(url)
        if daemon_id:
            daemon_id = validate_daemon_id(daemon_id)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    daemon_id = daemon_id or str(uuid.uuid4())
    ws_port = _ws_port_from_url(url)

    if add_entry:
        try:
            outcome = upsert_daemons_entry(
                DaemonEntry(name=name, url=url, daemon_id=daemon_id),
                replace_name=force,
            )
        except DaemonsFileError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(1)
        click.echo(
            f"{outcome.capitalize()} {name} {url} (id={daemon_id}) "
            f"in {daemons_file_path()}"
        )

    try:
        code = encode_invite(
            secret=secret,
            url=url,
            daemon_id=daemon_id,
            name=name,
            ws_port=ws_port,
            server_url=server_url,
        )
    except InviteError as exc:  # pragma: no cover — defensive
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo()
    click.echo("Run this on the daemon machine:")
    click.echo()
    click.echo(f"    agt daemon init {code}")
    click.echo()
    click.echo(
        "The code embeds your server secret. Treat it like an SSH private key."
    )


@daemon.command("init")
@click.argument("invite_code", required=False, metavar="[INVITE_CODE]")
@click.option(
    "--token",
    default=None,
    help="Server secret. Required when no INVITE_CODE is given.",
)
@click.option("--url", "url", default=None, help="This daemon's public ws:// URL.")
@click.option("--name", default=None, help="Daemon label.")
@click.option(
    "--daemon-id",
    "daemon_id",
    default=None,
    help="Explicit daemon id. Defaults to uuid5(url) when omitted.",
)
@click.option("--ws-port", type=int, default=None, help="Listen port (default 8422).")
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite an existing server secret that differs from the new one.",
)
@click.option(
    "--start",
    "start_after",
    is_flag=True,
    help="Start the daemon in the background after configuring it.",
)
@click.pass_context
def daemon_init(
    ctx: click.Context,
    invite_code: str | None,
    token: str | None,
    url: str | None,
    name: str | None,
    daemon_id: str | None,
    ws_port: int | None,
    force: bool,
    start_after: bool,
) -> None:
    """Configure this machine as a reverse-dial daemon.

    With an INVITE_CODE (from ``agt daemon invite`` on the Server) this
    writes the server secret, the daemon identity/URL and the listen
    settings in one step. Without one, pass ``--token`` and ``--url``
    explicitly.
    """
    import socket as _socket

    from ..daemon.config import (
        DaemonConfigError,
        update_daemon_config_file,
    )
    from ..daemons_file import (
        DaemonEntry,
        DaemonsFileError,
        daemons_file_path,
        upsert_daemons_entry,
    )
    from ..ids import daemon_id_from_url
    from ..invite import InviteError, decode_invite
    from ..server_secret import default_server_secret_path

    payload: dict = {}
    server_url: str | None = None
    if invite_code:
        try:
            payload = decode_invite(invite_code)
        except InviteError as exc:
            click.echo(f"Error: {exc}", err=True)
            sys.exit(2)
        secret = payload["secret"]
        url = payload["url"]
        daemon_id = payload["id"]
        name = payload.get("name") or name or _socket.gethostname()
        ws_port = payload.get("ws_port") or ws_port
        server_url = payload.get("server_url")
    else:
        if not token:
            click.echo(
                "Error: --token is required when no INVITE_CODE is given.",
                err=True,
            )
            sys.exit(2)
        if not url:
            click.echo("Error: --url is required.", err=True)
            sys.exit(2)
        secret = token
        daemon_id = daemon_id or daemon_id_from_url(url)
        name = name or _socket.gethostname()

    # The daemons file only accepts a UUID as ``id=``; catching a bad
    # value here beats writing an entry whose identity is silently
    # demoted to a capability later.
    try:
        from ..daemons_file import validate_daemon_id

        daemon_id = validate_daemon_id(daemon_id)
    except ValueError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    ws_port = ws_port or _ws_port_from_url(url or "")
    if url is None:
        click.echo("Error: no daemon URL configured.", err=True)
        sys.exit(2)

    # 1. Shared secret. Never clobber a different one by accident: that
    #    would silently disconnect a machine that is already paired.
    secret_path = default_server_secret_path()
    existing_secret = ""
    if secret_path.exists():
        try:
            existing_secret = secret_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            click.echo(f"Error: cannot read {secret_path}: {exc}", err=True)
            sys.exit(1)
    if existing_secret and existing_secret != secret and not force:
        click.echo(
            f"Error: {secret_path} already holds a different server secret.\n"
            "Re-run with --force to replace it (this machine will then only "
            "be able to talk to the new Server).",
            err=True,
        )
        sys.exit(1)
    if existing_secret != secret:
        try:
            secret_path.parent.mkdir(parents=True, exist_ok=True)
            secret_path.write_text(secret, encoding="utf-8")
            secret_path.chmod(0o600)
        except OSError as exc:
            click.echo(f"Error: cannot write {secret_path}: {exc}", err=True)
            sys.exit(1)
        click.echo(f"Wrote server secret to {secret_path}")

    # 2. Persisted daemon identity + listen settings.
    updates: dict = {
        "daemon_id": daemon_id,
        "daemon_url": url,
        "listen_mode": True,
        "ws_port": ws_port,
    }
    if server_url:
        updates["server_url"] = server_url
    try:
        update_daemon_config_file(updates)
    except DaemonConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    # 3. This machine's own entry, so `agt daemon status` / CLI helpers can
    #    still resolve the local daemon.
    try:
        upsert_daemons_entry(
            DaemonEntry(name=name, url=url, daemon_id=daemon_id),
            replace_name=force,
        )
    except DaemonsFileError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    click.echo(
        f"Configured {name} ({url}, id={daemon_id}) in {daemons_file_path()}"
    )

    if start_after:
        ctx.invoke(daemon_start, server_url=None, detach=True, log_file=None)
        return

    click.echo()
    click.echo("Next: agt daemon start --detach")
    click.echo(
        f"Make sure this machine's port {ws_port} is reachable from the Server."
    )


@daemon.command("list")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def daemon_list(server_url: str) -> None:
    """List every daemon known to the Server (Server view).

    Unlike ``agt daemon status`` (which checks the LOCAL pid file), this
    queries the Server's daemon registry: configured ``~/.agt/daemons``
    targets (even if never seen/offline) plus any runtime-only daemons.
    """

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_daemons()

    try:
        daemons = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not daemons:
        click.echo("No daemons registered on the Server.")
        return

    click.echo(f"🖥️  {len(daemons)} daemon(s) registered on the Server\n")
    click.echo(
        f"  {'NAME':<18} {'DAEMON_ID':<16} {'URL':<24} {'DEVICE':<18} "
        f"{'RUNTIME':<8} {'PROVIDERS':<18} {'CAPABILITIES':<22} "
        f"{'ONLINE':<8} {'LAST SEEN':<20}"
    )
    click.echo("  " + "─" * 150)
    for d in daemons:
        providers = ",".join(p["provider"] for p in d["providers"]) or "—"
        name = d.get("daemon_name") or "—"
        url = d.get("url") or "—"
        caps = ",".join(d.get("capabilities") or []) or "—"
        click.echo(
            f"  {name[:18]:<18} {d['daemon_id'][:16]:<16} {url[:24]:<24} "
            f"{(d.get('device_name') or '—')[:18]:<18} {d['runtime_count']:<8} "
            f"{providers[:18]:<18} {caps[:22]:<22} "
            f"{'yes' if d['online'] else 'no':<8} {(d.get('last_seen') or '—'):<20}"
        )


@daemon.command("show")
@click.argument("daemon_id")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def daemon_show(daemon_id: str, server_url: str) -> None:
    """Show one daemon's details from the Server registry."""

    async def _run() -> dict | None:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.get_daemon(daemon_id)

    try:
        daemon = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if daemon is None:
        click.echo(f"Daemon not found: {daemon_id}", err=True)
        sys.exit(2)

    click.echo(f"daemon_id    : {daemon['daemon_id']}")
    click.echo(f"daemon_name  : {daemon.get('daemon_name') or '—'}")
    click.echo(f"device_name  : {daemon.get('device_name') or '—'}")
    click.echo(f"online       : {'yes' if daemon['online'] else 'no'}")
    click.echo(f"last_seen    : {daemon.get('last_seen') or '—'}")
    click.echo(f"runtime_count: {daemon['runtime_count']}\n")
    click.echo(f"  {'PROVIDER':<12} {'VERSION':<12} {'STATUS':<10} {'LAST SEEN':<20}")
    click.echo("  " + "─" * 56)
    for p in daemon["providers"]:
        click.echo(
            f"  {p['provider'][:12]:<12} {p['version'][:12]:<12} "
            f"{p['status'][:10]:<10} {p['last_seen']:<20}"
        )


@daemon.command("bindings")
@click.argument("daemon_id")
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def daemon_bindings(daemon_id: str, server_url: str) -> None:
    """List every identity binding on a daemon (across all identities).

    Inverse view of ``agt identity bindings <did>``: instead of "which
    runtimes is this identity bound to", answers "which identities are
    bound to this daemon".
    """

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url,
            daemon_id="cli-direct",
        ) as client:
            return await client.list_daemon_bindings(daemon_id)

    try:
        bindings = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not bindings:
        click.echo(f"No bindings on daemon '{daemon_id}'.")
        return

    click.echo(f"🔗 {len(bindings)} binding(s) on daemon {daemon_id}\n")
    click.echo(
        f"  {'IDENTITY':<40} {'RUNTIME':<12} {'PRIORITY':<10} {'DEFAULT':<8}"
    )
    click.echo("  " + "─" * 72)
    for b in bindings:
        click.echo(
            f"  {b['identity_did'][:40]:<40} {b['provider'][:12]:<12} "
            f"{b['priority']:<10} {'yes' if b['is_default'] else '—':<8}"
        )


# ── daemon config (persistent execution settings) ────────────────────


@daemon.group("config")
def daemon_config() -> None:
    """Manage persistent Daemon execution-resource configuration."""


@daemon_config.command("show")
def daemon_config_show() -> None:
    """Show effective next-start Daemon configuration and its sources."""
    from ..daemon.config import (
        DaemonConfigError,
        daemon_config_file_path,
        resolve_daemon_settings,
    )
    from ..daemon.pidfile import read_daemon_pidfile

    path = daemon_config_file_path()
    click.echo(f"Configuration file: {path}")
    if not path.exists():
        click.echo("(file does not exist)\n")

    try:
        settings = resolve_daemon_settings()
    except DaemonConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    defaults = {
        "max_concurrent": "max concurrent tasks",
        "agent_timeout": "agent timeout (seconds)",
        "work_dir": "work directory",
        "agent_cwd_allowlist": "agent cwd allowlist",
        "listen_mode": "listen mode (reverse-dial)",
        "ws_port": "ws listen port",
        "daemon_id": "daemon id",
        "daemon_url": "daemon url",
        "server_url": "server url",
    }
    # Identity/URL keys are usually unset; printing four "source: default"
    # lines for them would bury the four settings this command existed for.
    optional_keys = {"daemon_id", "daemon_url", "server_url"}

    for key, label in defaults.items():
        rs = settings[key]
        if key in optional_keys and rs.source == "default":
            continue
        value = rs.value
        if isinstance(value, bool):
            display = "yes" if value else "no"
        elif isinstance(value, list):
            display = ":".join(str(p) for p in value)
        elif isinstance(value, Path):
            display = str(value)
        else:
            display = str(value)

        source = rs.source
        if source == "env" and rs.env_name:
            source = f"env {rs.env_name}"

        parts = [f"{label}: {display}", f"source: {source}"]
        if rs.file_value is not None and source != "file":
            if isinstance(rs.file_value, list):
                saved = ":".join(str(p) for p in rs.file_value)
            elif isinstance(rs.file_value, Path):
                saved = str(rs.file_value)
            else:
                saved = str(rs.file_value)
            parts.append(f"saved: {saved}")
        if (
            rs.default_value is not None
            and rs.default_value != ""
            and source != "default"
        ):
            if isinstance(rs.default_value, list):
                default = ":".join(str(p) for p in rs.default_value)
            elif isinstance(rs.default_value, Path):
                default = str(rs.default_value)
            elif isinstance(rs.default_value, bool):
                default = "yes" if rs.default_value else "no"
            else:
                default = str(rs.default_value)
            parts.append(f"default: {default}")
        click.echo("  ".join(parts))

    if read_daemon_pidfile() is not None:
        click.echo(
            "\nNote: a local Daemon is currently running; changes take effect after restart.",
            err=True,
        )


@daemon_config.command("set")
@click.option("--max-concurrent", type=int, default=None, help="Maximum concurrent agent tasks.")
@click.option("--agent-timeout", type=int, default=None, help="Agent subprocess timeout in seconds.")
@click.option("--work-dir", type=str, default=None, help="Absolute workspace root directory.")
@click.option(
    "--agent-cwd-allowlist",
    type=str,
    multiple=True,
    default=None,
    help="Absolute allowed agent-cwd root (repeatable).",
)
@click.option(
    "--listen-mode/--no-listen-mode",
    "listen_mode",
    default=None,
    help="Listen for the Server to dial in (reverse-dial) instead of dialing it.",
)
@click.option(
    "--ws-port",
    type=int,
    default=None,
    help="Reverse-dial listen port (default 8422).",
)
def daemon_config_set(
    max_concurrent: int | None,
    agent_timeout: int | None,
    work_dir: str | None,
    agent_cwd_allowlist: tuple[str, ...] | None,
    listen_mode: bool | None,
    ws_port: int | None,
) -> None:
    """Persist one or more Daemon execution-resource settings."""
    from ..daemon.config import (
        DaemonConfigError,
        _MANAGED_KEYS,
        daemon_config_file_path,
        resolve_daemon_settings,
        update_daemon_config_file,
    )
    from ..daemon.pidfile import read_daemon_pidfile

    updates: dict[str, Any] = {}
    if max_concurrent is not None:
        updates["max_concurrent"] = max_concurrent
    if agent_timeout is not None:
        updates["agent_timeout"] = agent_timeout
    if work_dir is not None:
        updates["work_dir"] = work_dir
    if agent_cwd_allowlist:
        updates["agent_cwd_allowlist"] = list(agent_cwd_allowlist)
    if listen_mode is not None:
        updates["listen_mode"] = listen_mode
    if ws_port is not None:
        updates["ws_port"] = ws_port

    if not updates:
        click.echo("Error: at least one setting is required.", err=True)
        sys.exit(2)

    try:
        update_daemon_config_file(updates)
    except DaemonConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    path = daemon_config_file_path()
    click.echo(f"Saved configuration to {path}")

    # Warn if any saved setting is currently overridden by an env var.
    resolved = resolve_daemon_settings()
    overridden: list[str] = []
    for key in updates:
        rs = resolved[key]
        if rs.source == "env" and rs.env_name:
            overridden.append(f"{key} (via {rs.env_name})")
    if overridden:
        click.echo(
            "Warning: the following saved values are overridden by current environment variables "
            "and will not take effect until those variables are unset:",
            err=True,
        )
        for item in overridden:
            click.echo(f"  - {item}", err=True)

    # Warn if the local daemon is running.
    if read_daemon_pidfile() is not None:
        click.echo(
            "Warning: the local Daemon is running; restart it for these changes to take effect.",
            err=True,
        )

    # Warn if / is in the allowlist.
    if "agent_cwd_allowlist" in updates:
        allowlist = updates["agent_cwd_allowlist"]
        if any(str(Path(p)) == "/" for p in allowlist):
            click.echo(
                "Warning: '/' in agent_cwd_allowlist allows the Daemon to run agents anywhere on "
                "the filesystem. Only use this when you trust every submitter.",
                err=True,
            )


@daemon_config.command("unset")
@click.option("--max-concurrent", is_flag=True, help="Remove max_concurrent from the file.")
@click.option("--agent-timeout", is_flag=True, help="Remove agent_timeout from the file.")
@click.option("--work-dir", is_flag=True, help="Remove work_dir from the file.")
@click.option(
    "--agent-cwd-allowlist", is_flag=True, help="Remove agent_cwd_allowlist from the file."
)
@click.option("--listen-mode", is_flag=True, help="Remove listen_mode from the file.")
@click.option("--ws-port", is_flag=True, help="Remove ws_port from the file.")
@click.option("--daemon-id", is_flag=True, help="Remove daemon_id from the file.")
@click.option("--daemon-url", is_flag=True, help="Remove daemon_url from the file.")
@click.option("--server-url", is_flag=True, help="Remove server_url from the file.")
def daemon_config_unset(
    max_concurrent: bool,
    agent_timeout: bool,
    work_dir: bool,
    agent_cwd_allowlist: bool,
    listen_mode: bool,
    ws_port: bool,
    daemon_id: bool,
    daemon_url: bool,
    server_url: bool,
) -> None:
    """Remove persisted Daemon execution-resource settings."""
    from ..daemon.config import (
        DaemonConfigError,
        daemon_config_file_path,
        remove_daemon_config_keys,
    )

    keys = [
        key
        for key, flag in {
            "max_concurrent": max_concurrent,
            "agent_timeout": agent_timeout,
            "work_dir": work_dir,
            "agent_cwd_allowlist": agent_cwd_allowlist,
            "listen_mode": listen_mode,
            "ws_port": ws_port,
            "daemon_id": daemon_id,
            "daemon_url": daemon_url,
            "server_url": server_url,
        }.items()
        if flag
    ]
    if not keys:
        click.echo("Error: at least one setting is required.", err=True)
        sys.exit(2)

    try:
        remove_daemon_config_keys(keys)
    except DaemonConfigError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(2)

    path = daemon_config_file_path()
    if path.exists():
        click.echo(f"Removed settings from {path}: {', '.join(keys)}")
    else:
        click.echo(f"Removed settings; {path} was deleted because no managed values remain.")


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
    from ..server.config import _load_server_secret

    secret = _load_server_secret()
    if not secret:
        click.echo(
            "Error: AGT_SERVER_SECRET is not configured. "
            "Run `agt server init` to generate one, or set the "
            "AGT_SERVER_SECRET environment variable.",
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


# ── artifact (Phase 2/3: retrieval chain) ─────────────────────────


def _parse_artifact_ref(ref: str) -> str:
    """Accept ``agt-artifact://<id>`` or a bare id; return the id."""
    return ref.removeprefix("agt-artifact://").removeprefix("agt-artifact:")


def _rpc_rejected_detail(exc: RPCRejected) -> str:
    """Pull the server's ``detail`` out of an RPCRejected, else the raw body.

    ``_get`` wraps non-JSON error bodies as ``{"raw": resp.text}``, so a
    403's ``detail`` (e.g. "session does not belong to daemon …") arrives
    as a JSON string we have to unwrap for a clean CLI message.
    """
    body = exc.body
    if isinstance(body, dict):
        raw = body.get("raw")
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("detail"):
                    return str(parsed["detail"])
            except (ValueError, TypeError):
                pass
        if body.get("detail"):
            return str(body["detail"])
    return str(body)


@cli.group()
def artifact() -> None:
    """View and download registered artifacts (talk to the Server)."""


@artifact.command("list")
@click.option(
    "--session", "session_id", default=None,
    help="Session id whose artifacts to list. Omit to list every artifact "
         "on the daemon (cross-session, with a SESSION column).",
)
@click.option(
    "--daemon", "daemon_id", default=None,
    help="Target daemon. Defaults to this machine's daemon; errors if none "
         "is found. With --session, ownership is enforced server-side.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def artifact_list(session_id: str | None, daemon_id: str | None, server_url: str) -> None:
    """List registered artifacts.

    With ``--session``: that session's artifacts (must belong to --daemon).
    Without ``--session``: every artifact on --daemon, each with a SESSION id.
    """
    if daemon_id is None:
        daemon_id = _local_daemon_id()
        if daemon_id is None:
            click.echo(
                "Error: no local daemon found; pass --daemon <id>.",
                err=True,
            )
            sys.exit(2)

    if session_id is not None:
        _print_session_artifacts(session_id, daemon_id, server_url)
    else:
        _print_daemon_artifacts(daemon_id, server_url)


def _print_session_artifacts(session_id: str, daemon_id: str, server_url: str) -> None:
    """List one session's artifacts (the session must belong to ``daemon_id``)."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url, daemon_id="cli-direct"
        ) as client:
            return await client.list_session_artifacts(session_id, daemon_id)

    try:
        artifacts = asyncio.run(_run())
    except RPCRejected as exc:
        click.echo(f"Error: {_rpc_rejected_detail(exc)}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not artifacts:
        click.echo(
            f"No artifacts for session {session_id[:8]}… on daemon '{daemon_id}'."
        )
        return

    click.echo(f"📦 {len(artifacts)} artifact(s) for session {session_id[:8]}…\n")
    click.echo(
        f"  {'NAME':<32} {'STATUS':<10} {'SIZE':<10} {'MIME':<24} URI"
    )
    click.echo("  " + "─" * 100)
    for a in artifacts:
        size = a.get("size")
        size_str = str(size) if size is not None else "—"
        click.echo(
            f"  {a['name'][:32]:<32} {a['status']:<10} {size_str:<10} "
            f"{(a.get('mime_type') or '—')[:24]:<24} {a['uri']}"
        )


def _print_daemon_artifacts(daemon_id: str, server_url: str) -> None:
    """List every artifact on a daemon, each with its producing SESSION id."""

    async def _run() -> list[dict]:
        async with ServerClient(
            base_url=server_url, daemon_id="cli-direct"
        ) as client:
            return await client.list_daemon_artifacts(daemon_id)

    try:
        artifacts = asyncio.run(_run())
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)

    if not artifacts:
        click.echo(f"No artifacts on daemon '{daemon_id}'.")
        return

    click.echo(f"📦 {len(artifacts)} artifact(s) on daemon '{daemon_id}'\n")
    rows = []
    for a in artifacts:
        size = a.get("size")
        rows.append((
            a.get("session_id") or "—",
            a.get("name") or "",
            a.get("status") or "",
            str(size) if size is not None else "—",
            a.get("uri") or "",
        ))
    widths = [
        max(len(r[0]) for r in rows),
        max(len(r[1]) for r in rows),
        max(len(r[2]) for r in rows),
        max(len(r[3]) for r in rows),
    ]
    header = (
        f"  {'SESSION':<{widths[0]}}  {'NAME':<{widths[1]}}  "
        f"{'STATUS':<{widths[2]}}  {'SIZE':<{widths[3]}}  URI"
    )
    click.echo(header)
    click.echo("  " + "─" * (sum(widths) + 2 * 4 + 3))
    for r in rows:
        click.echo(
            f"  {r[0]:<{widths[0]}}  {r[1]:<{widths[1]}}  "
            f"{r[2]:<{widths[2]}}  {r[3]:<{widths[3]}}  {r[4]}"
        )


def _single_dest(meta: dict, output: str | None) -> Path:
    """Destination path for a single download: -o wins, else the artifact's
    basename in the cwd."""
    if output:
        return Path(output)
    name = meta.get("name") or meta["artifact_id"]
    return Path(Path(name).name)


async def _download_one(
    client: ServerClient, artifact_id: str, output: str | None
) -> int:
    """Download one artifact's bytes; returns the process exit code."""
    meta = await client.get_artifact(artifact_id)
    if meta is None:
        click.echo(f"Error: artifact not found: {artifact_id}", err=True)
        return 2
    dest = _single_dest(meta, output)
    content = await client.get_artifact_content(artifact_id)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)
    click.echo(f"📥 {meta['uri']} → {dest} ({len(content)} bytes)")
    return 0


async def _download_batch(
    client: ServerClient, session_id: str, daemon_id: str, out_dir: str
) -> int:
    """Download every artifact in a session into ``out_dir`` (best-effort
    per artifact); returns the process exit code."""
    artifacts = await client.list_session_artifacts(session_id, daemon_id)
    if not artifacts:
        click.echo(
            f"No artifacts for session {session_id[:8]}… on daemon '{daemon_id}'."
        )
        return 0

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    for a in artifacts:
        artifact_id = a["artifact_id"]
        # ``name`` is a server-validated relative path inside outputs/;
        # preserve its subdirs but guard against traversal defensively.
        rel = Path(a.get("name") or artifact_id)
        if rel.is_absolute() or ".." in rel.parts:
            rel = Path(artifact_id)
        dest = out / rel
        try:
            content = await client.get_artifact_content(artifact_id)
        except RPCRejected as exc:
            click.echo(f"⚠️  {a['uri']}: {_rpc_rejected_detail(exc)}", err=True)
            failed += 1
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        click.echo(f"📥 {a['uri']} → {dest} ({len(content)} bytes)")
        ok += 1

    if failed:
        click.echo(f"{ok} downloaded, {failed} failed.", err=True)
        return 1
    click.echo(f"✅ {ok} artifact(s) → {out}")
    return 0


@artifact.command("get")
@click.argument("ref", required=False)
@click.option(
    "-o", "--output", default=None, type=click.Path(),
    help="Write the artifact bytes to this path (single mode).",
)
@click.option(
    "--session", "session_id", default=None,
    help="Batch: download every artifact in this session.",
)
@click.option(
    "--daemon", "daemon_id", default=None,
    help="Batch: daemon that owns the session.",
)
@click.option(
    "--out-dir", default=None, type=click.Path(),
    help="Batch: directory to write files into.",
)
@click.option(
    "--server-url",
    default="http://127.0.0.1:8420",
    show_default=True,
    envvar="AGT_SERVER_URL",
)
def artifact_get(
    ref: str | None,
    output: str | None,
    session_id: str | None,
    daemon_id: str | None,
    out_dir: str | None,
    server_url: str,
) -> None:
    """Download artifact bytes.

    Single: ``agt artifact get <id|agt-artifact://…> [-o PATH]``.
    Batch: ``agt artifact get --session <sid> --daemon <id> --out-dir DIR``.
    """
    batch = session_id is not None
    if batch:
        if ref is not None:
            click.echo("Error: pass either a REF or --session, not both.", err=True)
            sys.exit(2)
        if not daemon_id:
            click.echo("Error: --session requires --daemon.", err=True)
            sys.exit(2)
        if not out_dir:
            click.echo("Error: --session requires --out-dir.", err=True)
            sys.exit(2)
        if output is not None:
            click.echo(
                "Error: -o/--output is single-mode; use --out-dir in batch.",
                err=True,
            )
            sys.exit(2)
    elif ref is None:
        click.echo(
            "Error: provide a REF (id or agt-artifact://…) or use --session.",
            err=True,
        )
        sys.exit(2)

    async def _run() -> int:
        async with ServerClient(
            base_url=server_url, daemon_id="cli-direct"
        ) as client:
            if batch:
                return await _download_batch(client, session_id, daemon_id, out_dir)
            return await _download_one(client, _parse_artifact_ref(ref), output)

    try:
        code = asyncio.run(_run())
    except RPCRejected as exc:
        click.echo(f"Error: {_rpc_rejected_detail(exc)}", err=True)
        sys.exit(1)
    except Exception as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    sys.exit(code)


# Phase 5: SOP static-DAG commands.
from .sop import sop as _sop_group  # noqa: E402

cli.add_command(_sop_group)

# Goal mode: dynamic orchestration commands.
from .goal import goal as _goal_group  # noqa: E402

cli.add_command(_goal_group)


@cli.group()
def context() -> None:
    """Goal shared-context operations."""


@context.command("read")
@click.option("--key", "keys", multiple=True, help="Canonical context key.")
@click.option("--server-url", envvar="AGT_SERVER_URL", default=None)
@click.option("--json-output/--no-json", default=True, show_default=True)
def context_read(keys: tuple[str, ...], server_url: str | None, json_output: bool) -> None:
    """Read context visible to this Agent task (uses AGT_TASK_TOKEN)."""
    token = os.environ.get("AGT_TASK_TOKEN")
    task_id = os.environ.get("AGT_TASK_ID")
    base_url = server_url or os.environ.get("AGT_SERVER_URL") or "http://127.0.0.1:8420"
    if not token or not task_id:
        click.echo("Error: AGT_TASK_TOKEN and AGT_TASK_ID are required", err=True)
        sys.exit(2)

    async def _run() -> dict:
        params: list[tuple[str, str]] = []
        for key in keys:
            params.append(("keys", key))
        async with httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=15.0) as client:
            resp = await client.get(
                "/api/agent/context",
                params=params,
                headers={"X-Task-Token": token, "X-Task-Id": task_id},
            )
            if resp.status_code >= 300:
                raise CLIError(f"context read failed ({resp.status_code}): {resp.text}")
            return resp.json()

    try:
        result = asyncio.run(_run())
    except CLIError as exc:
        click.echo(f"Error: {exc}", err=True)
        sys.exit(1)
    if json_output:
        click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        for entry in result.get("entries", []):
            click.echo(f"{entry['entry_id']}  {entry['kind']}  {entry['key']}")


__all__ = ["cli"]
