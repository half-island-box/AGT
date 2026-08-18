"""Daemon — central controller that orchestrates probes, runtimes, and tasks."""

from __future__ import annotations

import uuid

from ..probe.scanner import AgentProbe
from ..runtime.models import AgentEntry, Runtime
from ..task.executor import TaskExecutor
from ..task.models import Task, TaskResult
from ..task.session import SessionStore
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
        session_store: SessionStore | None = None,
    ) -> None:
        self.config = config or DaemonConfig.from_env()
        self.probe = probe or AgentProbe()
        self.session_store = session_store or SessionStore()
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
    ) -> TaskResult:
        """Execute one prompt against the named provider.

        ``task_id`` lets callers (e.g. ``agt chat``) keep the same logical task
        across multiple runs so the same Claude Code session is resumed.

        ``resume_session_id`` overrides SessionStore lookup; used by the
        background daemon when Server provides the session pointer in the
        task payload.

        ``interactive=True`` makes the agent inherit the parent's stdio so
        it sees a TTY (some CLIs like ``codex`` refuse to run otherwise).
        Session continuity is then disabled — no session_id is captured
        and SessionStore isn't updated.
        """
        if not self._started:
            await self.start()

        runtime = self._runtimes.get(provider)
        if runtime is None:
            raise RuntimeError(
                f"Provider '{provider}' not found. Run `agt probe` to scan."
            )

        effective_task_id = task_id or uuid.uuid4().hex[:8]

        # Resolve the resume session: explicit override wins, else SessionStore.
        # Skipped in interactive mode (no session_id captured, so resume is moot).
        if not interactive and resume_session_id is None and task_id is not None:
            resume_session_id = self.session_store.get(effective_task_id, provider)

        task = Task(
            prompt=prompt,
            provider=provider,
            task_id=effective_task_id,
            work_dir=self.config.work_dir_root / provider / effective_task_id,
            resume_session_id=resume_session_id,
        )

        executor = TaskExecutor(runtime=runtime, config=self.config)
        result = await executor.execute(task, interactive=interactive)

        # Update SessionStore based on the outcome (only when caller opted
        # into session continuity by passing a task_id AND we captured a
        # session_id, which requires pipe mode).
        if task_id is not None and not interactive:
            if result.resume_rejected:
                self.session_store.delete(task_id)
            elif result.session_id:
                self.session_store.save(task_id, provider, result.session_id)

        return result

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