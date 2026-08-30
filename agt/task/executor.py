"""TaskExecutor — spawn an Agent CLI subprocess to run a single Task.

Three execution modes:

- **Streaming** (``sink != NoopSink``): consume structured JSON events
  from the agent CLI's stream output; print text deltas to the terminal
  in real time. Used when the provider supports streaming (Phase 5:
  Claude Code via ``--output-format stream-json``).
- **Pipe text** (``sink == NoopSink``, default for unknown providers):
  capture stdout/stderr with :class:`StreamReader`; extract session id
  from text + disk fallback. Legacy Phase 1 behaviour, preserved
  unchanged.
- **Interactive** (``interactive=True``): inherit parent stdio so the
  agent sees a real TTY. Trades session-id capture for the agent's
  native interactive UI.

The provider-specific bits (command-line shape, stream sink, disk
fallback, resume-rejection signal) live in
:mod:`agt.runtime.registry` — the executor only knows the abstract
``ProviderAdapter`` API and never references a provider name.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import time
from pathlib import Path

from ..daemon.config import DaemonConfig
from ..daemon.identity_inject import _inject_identity, _reclaim_identity
from ..runtime.models import Runtime
from ..runtime.registry import (
    ProviderAdapter,
    get_adapter,
)
from ..runtime.session_locators import SessionLocator
from ..runtime.streaming import (
    ClaudeStreamSink,
    NoopSink,
    StreamingSink,
)
from ..server.services.auth_service import TaskToken
from .models import Task, TaskResult
from .stream import StreamReader

log = logging.getLogger(__name__)

# Progress sampler cadence. Module-level (not config) so tests can patch it
# without touching DaemonConfig; mirrors the ``_monotonic`` seam in
# ``agt.daemon.ws_claim``.
_PROGRESS_FLUSH_INTERVAL = 0.5


# ── Legacy text-regex helpers ────────────────────────────────────────
#
# Kept as a fallback for providers on the pipe-text path (cursor /
# copilot / qwen / kimi / opencode / openclaw and any future provider
# without a streaming implementation). Phase 5 streaming providers
# (Claude Code) extract session_id / resume-rejection from structured
# event fields instead — these regexes are not consulted on the
# streaming path.
#
# Marked deprecated so a future change can delete them once the last
# remaining provider moves off the pipe-text path.

# Phrases observed in Claude Code output when --resume cannot find the session.
_RESUME_REJECT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"no session found", re.IGNORECASE),
    re.compile(r"session not found", re.IGNORECASE),
    re.compile(r"cannot resume", re.IGNORECASE),
    re.compile(r"resume was rejected", re.IGNORECASE),
    re.compile(r"failed to resume", re.IGNORECASE),
)


# Patterns to extract a fresh session id from agent output.
_SESSION_ID_REGEXES: tuple[re.Pattern[str], ...] = (
    re.compile(r"session[_\s-]?id[:\s=]+([A-Za-z0-9_-]{6,})", re.IGNORECASE),
    re.compile(r"\b(?:created|new)\s+session[:\s]+([A-Za-z0-9_-]{6,})", re.IGNORECASE),
)

# A full UUID — the session id embedded in Codex rollout filenames
# (``rollout-<ts>-<uuid>.jsonl``). Kept for any external importer; the
# internal Codex disk fallback now goes through
# :func:`agt.runtime.session_locators.codex_session_locator`.
_UUID_RE = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _detect_resume_rejected(stderr: str, exit_code: int) -> bool:
    """Deprecated text-regex resume rejection detector (pipe-text path only).

    Will be removed once the pipe-text path is retired; new code should
    use :class:`agt.runtime.resume_detectors.ResumeRejectionDetector`.
    """
    if exit_code == 0:
        return False
    return any(p.search(stderr) for p in _RESUME_REJECT_PATTERNS)


def _extract_session_id_from_output(stdout: str) -> str | None:
    """Deprecated text-regex session-id extractor (pipe-text path only).

    Will be removed once the pipe-text path is retired; the streaming
    path uses the structured ``result.session_id`` field from
    :class:`agt.runtime.streaming.ClaudeStreamSink` and providers on
    the pipe-text path fall back to ``_disk_fallback``.
    """
    for pattern in _SESSION_ID_REGEXES:
        match = pattern.search(stdout)
        if match:
            return match.group(1)
    return None


# ── Phase 5: provider-agnostic disk fallback ───────────────────────────


def _disk_fallback(locator: SessionLocator | None) -> str | None:
    """Resolve a fresh ``session_id`` from the provider's disk layout.

    Walks ``locator.roots`` recursively, applies ``locator.glob``, and
    extracts an id via ``locator.id_extractor`` for each match. Returns
    the most-recently-modified session id, or ``None`` when no locator
    is registered or no candidates are found.
    """
    if locator is None:
        return None
    candidates: list[tuple[float, str]] = []
    for root in locator.roots:
        if not root.exists():
            continue
        for path in root.rglob(locator.glob):
            try:
                sid = locator.id_extractor(path)
            except Exception:
                continue
            if sid is None:
                continue
            try:
                candidates.append((path.stat().st_mtime, sid))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return candidates[0][1]


# ── Phase 5: streaming args dispatch ──────────────────────────────────


def _stream_args_for(sink: StreamingSink) -> list[str]:
    """Return the argv additions to enable streaming for ``sink``.

    Looks at the sink's concrete type so the registry stays the single
    source of truth for "which provider supports what". Unknown sinks
    get an empty list (the executor falls back to the pipe-text path).

    Note: Claude Code requires ``--verbose`` to honour
    ``--output-format=stream-json`` under ``-p`` mode; without it the
    CLI refuses with "When using --print, --output-format=stream-json
    requires --verbose".
    """
    if isinstance(sink, ClaudeStreamSink):
        # ``--include-partial-messages`` enables Anthropic Messages
        # streaming events (``content_block_delta`` / ``text_delta``)
        # so the CLI emits each chunk as it is generated rather than
        # buffering into one ``assistant`` event at the end. Without
        # this flag the sink would still work (legacy ``assistant``
        # full-message events), but the terminal experience would
        # degrade to "wait for the full message, then dump" — see
        # phase5-local-streaming-design.md §A.
        return [
            "--output-format",
            "stream-json",
            "--verbose",
            "--include-partial-messages",
        ]
    return []


class AgentNotFoundError(RuntimeError):
    """The binary file referenced by the Runtime is missing."""


class TaskExecutor:
    """Run a single Task by spawning one agent subprocess."""

    def __init__(self, runtime: Runtime, config: DaemonConfig) -> None:
        self._runtime = runtime
        self._config = config

    # ── Public entry point ──────────────────────────────────────

    async def execute(
        self,
        task: Task,
        *,
        interactive: bool = False,
        progress_cb=None,
    ) -> TaskResult:
        """Run one agent subprocess for ``task``.

        Dispatches to one of three private helpers based on the
        provider's adapter:

        - :meth:`_execute_interactive` — when ``interactive=True``
        - :meth:`_execute_streaming` — when the provider's adapter
          returns a non-``NoopSink`` (e.g. Claude Code with
          ``--output-format stream-json``)
        - :meth:`_execute_pipe_text` — legacy Phase 1 path for providers
          without a streaming implementation

        Identity injection (Phase 4 Step 4) wraps the spawn regardless
        of mode; the injected files are reclaimed in ``finally``.
        """
        binary = self._runtime.agent_entry.path
        if not binary.exists():
            raise AgentNotFoundError(
                f"Agent binary not found at {binary}; run `agt probe` to re-scan."
            )

        adapter = get_adapter(self._runtime.agent_entry.provider)
        sink = adapter.stream_sink_factory()
        stream_args = _stream_args_for(sink)
        cmd = adapter.command_builder.build(binary, task, stream_args)

        work_dir = self._ensure_work_dir(task)
        env = self._build_env(task, work_dir)

        # Phase 4 Step 4: stack the agent's AGENTS.md into the spawn cwd
        # as ``IDENTITY.md`` + a marker block in the provider's
        # instructions file (``CLAUDE.md`` for claude, else
        # ``AGENTS.md``). Reclaimed in ``finally`` regardless of how the
        # spawn exits (timeout, crash, normal exit, interactive).
        #
        # Option D: when ``task.identity_md`` is set, the server already
        # rendered the identity and bundled the body into the claim
        # payload. We write it verbatim — no fetch from the Server,
        # which would be unreachable in a reverse-dial setup.
        if task.identity_did:
            _inject_identity(
                work_dir,
                task.identity_did,
                provider=task.provider,
                identity_md=task.identity_md,
            )

        start = time.monotonic()
        try:
            if interactive:
                return await self._execute_interactive(task, cmd, work_dir, env, start)
            if not isinstance(sink, NoopSink):
                return await self._execute_streaming(
                    task, cmd, work_dir, env, start, sink, adapter,
                    progress_cb=progress_cb,
                )
            return await self._execute_pipe_text(task, cmd, work_dir, env, start)
        finally:
            # Phase 4 Step 4: always reclaim the injected identity
            # files, regardless of exit path (timeout, crash, normal,
            # interactive). ``_reclaim_identity`` is a no-op when there
            # is no ``.agt-identity-managed`` sidecar or no AGENTS.md
            # marker block, so it's safe even when ``inject`` didn't run.
            if task.identity_did:
                _reclaim_identity(work_dir, did=task.identity_did)

    # ── Mode: interactive ───────────────────────────────────────

    async def _execute_interactive(
        self,
        task: Task,
        cmd: list[str],
        work_dir: Path,
        env: dict[str, str],
        start: float,
    ) -> TaskResult:
        """Spawn the agent inheriting the parent's stdio (TTY path).

        No stdout/stderr capture → no session-id extraction. The agent
        CLI sees a real terminal and can run interactive TUIs. Enforced
        ``agent_timeout`` upper bounds wall-clock so a hung agent
        doesn't pin the CLI forever; user Ctrl-C still works because
        the inherited TTY delivers the signal to the subprocess too.
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(work_dir),
            env=env,
        )
        timeout = self._config.agent_timeout
        try:
            exit_code = await asyncio.wait_for(process.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.task_id,
                provider=task.provider,
                exit_code=-1,
                stdout="",
                stderr="",
                duration=duration,
                status="timeout",
                session_id=None,
                resume_rejected=False,
            )
        duration = time.monotonic() - start
        return TaskResult(
            task_id=task.task_id,
            provider=task.provider,
            exit_code=exit_code,
            stdout="",
            stderr="",
            duration=duration,
            status="completed" if exit_code == 0 else "failed",
            session_id=None,
            resume_rejected=False,
        )

    # ── Mode: streaming (Phase 5) ────────────────────────────────

    async def _execute_streaming(
        self,
        task: Task,
        cmd: list[str],
        work_dir: Path,
        env: dict[str, str],
        start: float,
        sink: StreamingSink,
        adapter: ProviderAdapter,
        *,
        progress_cb=None,
    ) -> TaskResult:
        """Spawn the agent and consume its structured event stream.

        Each line of stdout is parsed as JSON and handed to ``sink``.
        ``sink.on_text_delta`` prints text fragments to the terminal in
        real time; ``sink.finalize`` yields the terminal
        :class:`SinkResult` carrying ``session_id`` and ``is_error``.
        The executor then:

        - falls back to ``_disk_fallback(adapter.session_locator)`` when
          the sink could not extract a ``session_id`` and we did not
          request a specific one (so a brand-new session has a fresh
          pointer for the next ``--resume``).
        - delegates resume-rejection detection to
          ``adapter.resume_rejection_detector``, which inspects the
          structured ``result`` event from the sink rather than stderr
          text.

        ``TaskResult.stdout`` is the plain-text concatenation the sink
        collected — the legacy "stdout is plain text" contract is
        preserved so downstream consumers (``/api/tasks/<id>/status``,
        ``_echo_task_result``, report_service) see no shape change.

        ``progress_cb`` (Phase 5 remote streaming), when provided, is an
        ``async(current_prefix) -> None`` callback invoked by a background
        sampler every ``_PROGRESS_FLUSH_INTERVAL`` seconds while the
        cumulative stdout prefix grows. It never affects the terminal
        ``TaskResult`` — the sampler is cancelled in ``finally`` and its
        exceptions are swallowed (see design decision 10).
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=env,
        )

        stdout_task = asyncio.create_task(self._drain_json(process.stdout, sink))
        stderr_task = asyncio.create_task(self._drain_silent(process.stderr))

        sampler_task = None
        if progress_cb is not None:
            sampler_task = asyncio.create_task(
                self._sample_progress(sink, progress_cb)
            )

        timeout = self._config.agent_timeout
        try:
            await asyncio.wait_for(
                asyncio.gather(stdout_task, stderr_task),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.task_id,
                provider=task.provider,
                exit_code=-1,
                stdout="",
                stderr="",
                duration=duration,
                status="timeout",
            )
        finally:
            if sampler_task is not None:
                sampler_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await sampler_task

        await process.wait()
        duration = time.monotonic() - start
        exit_code = process.returncode if process.returncode is not None else -1

        result = sink.finalize()
        session_id, resume_rejected = self._resolve_streaming_outcome(
            task=task,
            result=result,
            sink=sink,
            adapter=adapter,
        )

        return TaskResult(
            task_id=task.task_id,
            provider=task.provider,
            exit_code=exit_code,
            stdout=result.text,
            stderr="",
            duration=duration,
            status="completed" if exit_code == 0 else "failed",
            session_id=session_id,
            resume_rejected=resume_rejected,
        )

    async def _sample_progress(self, sink: StreamingSink, progress_cb) -> None:
        """Periodically report the sink's cumulative stdout prefix.

        Runs as a background task for the duration of the streaming
        execution. Reports the **full prefix** (idempotency requirement on
        the server side — a lost prefix can always be rebuilt by the next
        sample), only when it grew since the previous sample. ``progress_cb``
        exceptions are swallowed so a network blip on the report path can
        never crash the executor or leave an un-retrieved task exception.
        """
        last_len = 0
        while True:
            await asyncio.sleep(_PROGRESS_FLUSH_INTERVAL)
            current = "".join(sink.accumulated_text)
            if len(current) <= last_len:
                continue
            last_len = len(current)
            try:
                await progress_cb(current)
            except Exception:
                log.debug(
                    "progress_cb raised; skipping this sample",
                    exc_info=True,
                )

    @staticmethod
    def _resolve_streaming_outcome(
        *,
        task: Task,
        result,
        sink: StreamingSink,
        adapter: ProviderAdapter,
    ) -> tuple[str | None, bool]:
        """Resolve final ``session_id`` and ``resume_rejected`` from a sink result.

        Pulled out of :meth:`_execute_streaming` so the dispatch logic in
        :meth:`execute` stays under the cognitive-complexity cap.
        """
        # When resuming we keep the requested sid on success; on rejection
        # (detector below) we drop it. For a brand-new session we prefer
        # the sink's structured field, falling back to the provider's
        # disk layout.
        if task.resume_session_id is not None and not result.is_error:
            session_id: str | None = task.resume_session_id
        else:
            session_id = result.session_id
            if not session_id and task.resume_session_id is None:
                session_id = _disk_fallback(adapter.session_locator)

        # Resume-rejection detector: inspect the structured result event.
        # We hand it the last "result" event by re-deriving the relevant
        # fields from the sink. ``ClaudeStreamSink`` exposes ``subtype``
        # and ``errors``; detectors for other providers can ignore them.
        raw_event: dict | None = None
        raw_error: dict | None = None
        if isinstance(sink, ClaudeStreamSink):
            raw_event = {
                "subtype": sink.subtype,
                "errors": list(sink.errors),
            }
        resume_rejected = bool(
            result.is_error
            and adapter.resume_rejection_detector.is_resume_rejection(
                is_error=result.is_error,
                raw_error=raw_error,
                raw_event=raw_event,
            )
        )

        # When the detector says the resume was rejected, the requested
        # sid is unusable — clear it so the next claim starts fresh.
        if resume_rejected:
            session_id = None
        return session_id, resume_rejected

    async def _drain_json(self, stream, sink: StreamingSink) -> None:
        """Read JSON Lines from ``stream`` and feed each event to ``sink``.

        Tolerant by design: malformed JSON lines and exceptions raised
        by ``sink.consume`` are logged at warning and skipped, so a
        single bad frame cannot break the stream.

        Uses :meth:`_read_lines` so there is no per-line limit (the
        default ``StreamReader.readline`` 64 KiB cap is too small for
        stream-json events that contain a long ``text`` block).
        """
        async for raw_line in self._read_lines(stream):
            try:
                obj = json.loads(raw_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            try:
                sink.consume(obj)
            except Exception as exc:
                log.warning("sink.consume raised; dropping event: %s", exc)

    async def _read_lines(self, stream):
        """Yield one line at a time from ``stream`` with no per-line limit.

        Reads fixed-size chunks (64 KiB) and splits on newlines; any
        incomplete trailing line at EOF is also yielded. Both the
        streaming and pipe-text paths use this so neither hits the
        ``StreamReader.readline`` 64 KiB cap.
        """
        buf = b""
        chunk_size = 64 * 1024
        while True:
            try:
                chunk = await stream.read(chunk_size)
            except ValueError:
                log.warning("stream.read raised; aborting drain")
                return
            if not chunk:
                if buf.strip():
                    yield buf
                return
            buf += chunk
            while b"\n" in buf:
                line_bytes, _, buf = buf.partition(b"\n")
                if line_bytes.strip():
                    yield line_bytes

    async def _drain_silent(self, stream) -> None:
        """Read ``stream`` to EOF and discard its content.

        Agent CLIs (notably codex) print banner / diagnostics on stderr.
        Surfacing those on the live terminal would interleave them with
        the streaming text deltas and break the visual rhythm; we
        silently consume instead. ``TaskResult.stderr`` stays empty for
        now — extending it is a follow-up.
        """
        async for _ in self._read_lines(stream):
            pass

    async def _drain_silent_lines(self, stream, sink: list[str]) -> None:
        """Like :meth:`_drain_silent` but collects lines into ``sink``.

        Used by the legacy pipe-text path so ``result.stderr`` is
        populated for reporting / debugging.
        """
        async for line in self._read_lines(stream):
            sink.append(line.decode("utf-8", errors="replace"))

    async def _read_to_string(self, stream, on_line) -> str:
        """Read ``stream`` to EOF, calling ``on_line(line_str)`` per line.

        Uses :meth:`_read_lines` so lines longer than 64 KiB are
        handled correctly. Returns the concatenated string.
        """
        chunks: list[str] = []
        async for raw in self._read_lines(stream):
            text = raw.decode("utf-8", errors="replace")
            chunks.append(text)
            on_line(text)
        return "".join(chunks)

    # ── Mode: pipe text (legacy Phase 1, unchanged behaviour) ────

    async def _execute_pipe_text(
        self,
        task: Task,
        cmd: list[str],
        work_dir: Path,
        env: dict[str, str],
        start: float,
    ) -> TaskResult:
        """Legacy Phase 1 execution path (still used by non-streaming providers).

        Reads stdout/stderr via the chunked :meth :meth:`_read_lines`
        helper (no per-line 64 KiB cap like the default
        ``StreamReader.readline``), extracts the session id with text
        regex (deprecated — kept as a fallback for providers on this
        path) plus the adapter's ``session_locator`` for a disk-side
        fallback, and detects resume rejection via stderr regex
        (deprecated). Behaviour is intentionally close to the
        pre-Phase-5 implementation so providers without streaming keep
        working without regression.
        """
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(work_dir),
            env=env,
        )

        # Print stdout live (the agent's answer streams to the terminal
        # line-by-line) and silence stderr on the live terminal: agent
        # CLIs (notably the non-streaming Codex path) print their banner
        # + diagnostics on stderr, which would spam the user's chat.
        # Both streams are still captured — stdout for ``result.stdout``
        # (contract A), stderr for reporting / debugging. This mirrors
        # the pre-Phase-5 ``StreamReader`` behaviour; the silent
        # ``_read_lines`` collector dropped the live print and mangled
        # newlines (see phase5-local-streaming-design.md §4.6.2).
        reader = StreamReader(
            stdout=process.stdout,
            stderr=process.stderr,
            on_stderr=lambda _line: None,
        )

        timeout = self._config.agent_timeout
        try:
            stdout, stderr = await asyncio.wait_for(
                reader.read_all(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            try:
                await process.wait()
            except ProcessLookupError:
                pass
            duration = time.monotonic() - start
            return TaskResult(
                task_id=task.task_id,
                provider=task.provider,
                exit_code=-1,
                stdout="",
                stderr="",
                duration=duration,
                status="timeout",
            )

        await process.wait()
        duration = time.monotonic() - start
        exit_code = process.returncode if process.returncode is not None else -1

        # Decide the resume outcome based on what actually happened.
        if task.resume_session_id is not None:
            if _detect_resume_rejected(stderr, exit_code):
                session_id: str | None = None
                resume_rejected = True
            else:
                session_id = task.resume_session_id
                resume_rejected = False
        else:
            # Text-regex first (legacy), then the adapter's disk locator.
            adapter = get_adapter(task.provider)
            session_id = _extract_session_id_from_output(stdout + "\n" + stderr)
            if not session_id:
                session_id = _disk_fallback(adapter.session_locator)
            resume_rejected = False

        if exit_code == 0:
            status = "completed"
        else:
            status = "failed"

        return TaskResult(
            task_id=task.task_id,
            provider=task.provider,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration=duration,
            status=status,
            session_id=session_id,
            resume_rejected=resume_rejected,
        )

    # ── Env / workdir preparation ───────────────────────────────

    def _build_env(self, task: Task, work_dir: Path | None = None) -> dict[str, str]:
        env = os.environ.copy()
        # Hint to Claude Code (and friends) that no TTY is attached.
        env.setdefault("CLAUDE_CODE_HEADLESS", "1")
        # Pass the daemon's local resource port to spawned agents so they can
        # call back via 127.0.0.1 for repo checkout etc.
        daemon_port = getattr(self._config, "daemon_port", None)
        if daemon_port:
            env.setdefault("AGT_DAEMON_PORT", str(daemon_port))
        # Token for X-Daemon-Local-Token on LocalResourceServer endpoints.
        local_token = getattr(self._config, "local_token", "")
        if local_token:
            env.setdefault("AGT_DAEMON_TOKEN", local_token)
        # Inject a task-scoped HMAC token so the agent subprocess can call
        # back to the Server (e.g. POST /api/agent/callback). The Server
        # validates X-Task-Token + X-Task-Id against the shared secret.
        agent_id = getattr(self._config, "daemon_id", "anonymous")
        workspace_id = str(task.work_dir) if task.work_dir else ""
        secret = getattr(self._config, "server_secret", "")
        if secret:
            token = TaskToken.create(
                agent_id=agent_id,
                task_id=task.task_id,
                workspace_id=workspace_id,
                secret=secret,
            )
            env["AGT_TASK_TOKEN"] = token
            env["AGT_SERVER_URL"] = self._config.server_url
            env["AGT_TASK_ID"] = task.task_id
        # Phase 4 Step 4: tell the agent where its identity's AGENTS.md
        # was injected. The file is ``IDENTITY.md`` in the spawn cwd; the
        # content is the identity's own AGENTS.md (a copy). Agents that
        # read this env (Claude Code / Codex hooks, AGT-MCP) get a role
        # pointer without AGT having to mutate the cwd.
        if task.identity_did:
            env["AGT_IDENTITY_FILE"] = str(work_dir / "IDENTITY.md")
        return env

    def _ensure_work_dir(self, task: Task) -> Path:
        """Prepare ``task.work_dir`` and return the directory the agent
        subprocess should spawn in.

        Returns ``task.agent_cwd`` (the user's cwd at submit time) when
        set, else falls back to ``task.work_dir`` (legacy single-path
        mode). When ``task.work_dir`` is ``None`` we use the daemon's
        own cwd as a last-resort safety net — the caller's contract is
        to always set at least one of the two.
        """
        # Phase 4 workdir: when an explicit meta dir is set (e.g.
        # ``--work-dir`` override), ensure it exists before the
        # subprocess spawns. Bare ``agt chat`` leaves ``task.work_dir``
        # as ``None`` and we skip the mkdir entirely — nothing is ever
        # written under it. The agent cwd is kept *separate* from any
        # meta dir; we never write bookkeeping files there.
        if task.work_dir is not None:
            task.work_dir.mkdir(parents=True, exist_ok=True)
        # cwd injection: ``agent_cwd`` may name a directory on the daemon
        # that doesn't exist yet (e.g. ``agt chat --dir /root/project``
        # where the daemon hasn't created it). Create it on demand; a
        # permission error propagates (surfaced as the task's stderr).
        if task.agent_cwd is not None:
            task.agent_cwd.mkdir(parents=True, exist_ok=True)
        return task.agent_cwd or task.work_dir or Path.cwd()


__all__ = ["TaskExecutor", "AgentNotFoundError"]