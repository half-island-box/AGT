"""Daemon — central controller that orchestrates probes, runtimes, and tasks."""

from __future__ import annotations

import uuid
from pathlib import Path

from ..agent_history import load_session_title
from ..probe.scanner import AgentProbe
from ..runtime.models import AgentEntry, Runtime
from ..task.executor import TaskExecutor
from ..task.models import Task, TaskResult
from ..task.session import CwdSessionStore
from .config import DaemonConfig


class Daemon:
    """Coordinate local agent CLIs.

    Phase 1: lazily constructed per CLI invocation; no persistent server link.
    Phase 2: ``start()`` will additionally open the persistent server connection.
    """

    def __init__(
        self,
        config: DaemonConfig | None = None,
        probe: AgentProbe | None = None,
    ) -> None:
        self.config = config or DaemonConfig.from_env()
        self.probe = probe or AgentProbe()
        self._runtimes: dict[str, Runtime] = {}
        self._started = False

    # ── Lifecycle ───────────────────────────────────────────────

    async def start(self) -> None:
        """Scan agents and populate the runtime map."""
        if self._started:
            return
        entries = self.probe.scan()
        for entry in entries:
            self._runtimes[entry.provider] = self._build_runtime(entry)
        self._started = True
        # Phase 2: connect to server here.

    # ── Public API ──────────────────────────────────────────────

    async def probe_agents(self, force_refresh: bool = False) -> list[AgentEntry]:
        """Discover and return AgentEntries on this machine."""
        if force_refresh:
            entries = self.probe.force_refresh()
            # Rebuild runtime map from the fresh entries.
            self._runtimes = {e.provider: self._build_runtime(e) for e in entries}
            return entries
        await self.start()
        return self.probe.scan()

    def get_runtime(self, provider: str) -> Runtime | None:
        return self._runtimes.get(provider)

    def runtime_providers(self) -> list[str]:
        """Return the list of providers this daemon can run.

        Derived from the probe scan; used to filter claimable tasks to
        those whose provider we can actually execute (Step 3.4).
        """
        return list(self._runtimes.keys())

    async def run(
        self,
        provider: str = "claude",
        prompt: str = "",
        task_id: str | None = None,
        resume_session_id: str | None = None,
        *,
        interactive: bool = False,
        agent_cwd: Path | None = None,
        agt_meta_dir: Path | None = None,
        first_prompt: str | None = None,
        turn_index: int = 1,
        identity_did: str | None = None,
        identity_md: str | None = None,
        outputs_subdir: str | None = None,
        use_cwd_session_store: bool = False,
        progress_cb=None,
        event_cb=None,
    ) -> TaskResult:
        """Execute one prompt against the named provider.

        ``task_id`` lets callers (e.g. ``agt chat``) keep the same logical task
        across multiple runs so the same Claude Code session is resumed.

        ``resume_session_id`` is the provider session pointer. Server-managed
        tasks receive it from SQLite. Local direct chat may also pass it
        explicitly.

        ``interactive=True`` makes the agent inherit the parent's stdio so
        it sees a TTY (some CLIs like ``codex`` refuse to run otherwise).
        Session continuity is then disabled — no session_id is captured
        and the cwd store isn't updated.

        ``agent_cwd`` is the directory the agent subprocess runs in (its
        ``cwd``). Defaults to the user's cwd at submit time; falls back to
        ``agt_meta_dir`` when ``None`` (legacy single-path mode).

        ``use_cwd_session_store`` is deliberately explicit: only local
        direct chat enables ``<cwd>/.agt/sessions.json``. Server-managed
        chat, SOP and Goal tasks use SQLite and never touch that file.

        ``first_prompt`` is the original prompt of the chat session (used
        to label the session in ``agt chat --list``). ``turn_index`` is the
        1-based turn number being executed; we record it on the entry.

        ``identity_did`` (Phase 4 Step 4) declares which identity to
        inject. ``identity_md`` (Option D) is the server-rendered
        AGENTS.md body for that identity — passed through to
        ``TaskExecutor`` verbatim so the daemon never has to fetch from
        the Server (which is unreachable in reverse-dial). When
        ``identity_md`` is None, the executor falls back to reading
        ``~/.agt/identities/<slug>/AGENTS.md`` from disk.
        """
        if not self._started:
            await self.start()

        runtime = self._runtimes.get(provider)
        if runtime is None:
            raise RuntimeError(
                f"Provider '{provider}' not found. Run `agt probe` to scan."
            )

        effective_task_id = task_id or uuid.uuid4().hex[:8]

        if agt_meta_dir is not None:
            # Explicit meta dir (e.g. ``--work-dir`` override, ``--no-work-dir``
            # legacy, or a server-supplied value). Use it verbatim.
            meta_dir: Path | None = agt_meta_dir
        elif agent_cwd is not None:
            # Caller supplied the agent's cwd directly (e.g. bare ``agt chat``,
            # which stopped creating ``<cwd>/.agt/tasks/<id>/`` in this
            # iteration). No per-task meta dir. Local direct chat may use
            # ``<agent_cwd>/.agt/sessions.json`` when explicitly enabled.
            # We deliberately do NOT fall
            # back to ``work_dir_root`` here: that would silently re-create
            # the dead subdir under the daemon's home on a different machine.
            meta_dir = None
        else:
            # Neither given: legacy daemon-local layout.
            meta_dir = self.config.work_dir_root / provider / effective_task_id

        # cwd injection (Phase 4 workdir supplement): when the caller /
        # claim supplied an explicit agent_cwd (e.g. from ``agt chat --dir``,
        # or auto-detected by the CLI), honor it. Enforce the allowlist
        # *before* using it — never trust a path from the wire without a
        # sandbox check, even when the CLI already verified existence.
        # ``None`` → fall back to legacy ``meta_dir`` below.
        if agent_cwd is not None:
            from .config import _is_path_under
            allowlist = self.config.agent_cwd_allowlist or [Path.home()]
            if not any(
                _is_path_under(agent_cwd, root) for root in allowlist
            ):
                raise ValueError(
                    f"agent_cwd '{agent_cwd}' is outside the daemon's "
                    f"allowlist {allowlist}. "
                    f"Set AGT_AGENT_CWD_ALLOWLIST to widen."
                )

        effective_agent_cwd = agent_cwd or meta_dir or Path.cwd()

        # Phase 4 Step 4 + Option D: ``identity_md`` (when set) was
        # rendered by the Server at claim time and bundled into the
        # claim payload. We pass it through to ``TaskExecutor`` so the
        # daemon writes it directly into ``IDENTITY.md`` without any
        # outbound fetch to the Server (impossible in reverse-dial).
        # ``identity_did`` without ``identity_md`` falls back to reading
        # ``~/.agt/identities/<slug>/AGENTS.md`` from disk — covers
        # daemons running outside the server path.

        session_store = (
            self._resolve_session_store(effective_agent_cwd, provider)
            if use_cwd_session_store
            else None
        )
        if session_store is not None:
            resume_session_id = self._resolve_resume_session_id(
                session_store, resume_session_id, interactive, turn_index,
            )

        task = Task(
            prompt=prompt,
            provider=provider,
            task_id=effective_task_id,
            work_dir=meta_dir,
            agent_cwd=effective_agent_cwd,
            resume_session_id=resume_session_id,
            identity_did=identity_did,
            identity_md=identity_md,
            outputs_subdir=outputs_subdir,
        )

        executor = TaskExecutor(runtime=runtime, config=self.config)
        result = await executor.execute(
            task, interactive=interactive, progress_cb=progress_cb,
            event_cb=event_cb,
        )
        if result.session_id is not None and not result.resume_rejected:
            # Provider title stores are best-effort metadata caches. Read
            # after subprocess exit so a late AI-title write is visible;
            # failures leave the title unset and never fail execution.
            try:
                result.session_title = load_session_title(
                    provider, result.session_id
                )
            except Exception:
                result.session_title = None

        # Update the session store based on the outcome (only when caller
        # opted into session continuity by passing a task_id AND we
        # captured a session_id, which requires pipe mode).
        if (
            session_store is not None
            and task_id is not None
            and not interactive
        ):
            self._record_session_outcome(
                session_store,
                task_id=effective_task_id,
                prompt=prompt,
                first_prompt=first_prompt,
                turn_index=turn_index,
                resume_session_id=resume_session_id,
                result=result,
                identity_did=identity_did,
                title=result.session_title,
            )

        return result

    def _resolve_session_store(self, agent_cwd: Path, provider: str) -> CwdSessionStore:
        """Return the cwd-scoped session store for this run.

        ``agent_cwd`` is always resolved to a concrete directory by
        ``run`` (falls back to ``meta_dir``), so the store is keyed on
        that directory. One store file per cwd holds every chat session
        created there.
        """
        return CwdSessionStore.load_or_create(agent_cwd, provider=provider)

    @staticmethod
    def _resolve_resume_session_id(
        session_store: CwdSessionStore,
        explicit: str | None,
        interactive: bool,
        turn_index: int,
    ) -> str | None:
        """Pick the session_id to resume.

        Explicit (CLI ``--resume`` or Server claim payload) wins.
        Interactive mode never resumes (no session_id is captured).
        Otherwise, a chat turn *after the first* continues the session
        created by the prior turn — that's the store's most-recent
        entry. The first turn starts fresh unless ``--resume`` was given
        (bare ``agt chat`` = new session, per the clarified spec).
        """
        if interactive or explicit is not None:
            return explicit
        if turn_index > 1:
            last = session_store.last()
            if last is not None:
                return last.session_id
        return None

    @staticmethod
    def _record_session_outcome(
        session_store: CwdSessionStore,
        *,
        task_id: str,
        prompt: str,
        first_prompt: str | None,
        turn_index: int,
        resume_session_id: str | None,
        result: TaskResult,
        identity_did: str | None = None,
        title: str | None = None,
    ) -> None:
        """Persist the run outcome to the cwd store."""
        if result.resume_rejected:
            # Drop any record pointing at this (now-broken) sid so
            # future --resume doesn't pick a dead one.
            if resume_session_id:
                session_store.delete(resume_session_id)
            return
        if result.session_id:
            session_store.record(
                session_id=result.session_id,
                task_id=task_id,
                first_prompt=first_prompt or prompt,
                turn_count=turn_index,
                identity_did=identity_did,
                title=title,
            )

    # ── Internal helpers ────────────────────────────────────────

    def _build_runtime(self, entry: AgentEntry) -> Runtime:
        status = "online" if entry.is_available() else "offline"
        display = entry.display_name
        name = f"{display} ({self.config.device_name})"
        if self.config.runtime_name:
            name = f"{self.config.runtime_name} ({display})"
        return Runtime(
            name=name,
            type=entry.provider,
            version=entry.version,
            status=status,
            agent_entry=entry,
            device_name=self.config.device_name,
        )


__all__ = ["Daemon"]
