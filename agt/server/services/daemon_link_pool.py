"""DaemonLinkPool — owns the Server's reverse-dial links and reloads them.

The ``~/.agt/daemons`` file is the source of truth for which daemons this
Server dials. It used to be read exactly once, at construction, so a target
added while the Server was running needed a restart. :meth:`refresh` re-reads
it and reconciles the live links:

- a new target gets a link + run-loop task (``added``);
- a target whose URL changed is stopped and rebuilt (``replaced``);
- a target that disappeared is stopped and dropped (``removed``);
- an existing disconnected target is re-dialed *now* rather than after its
  up-to-60s backoff (``redialled``);
- a healthy link is never touched (``unchanged``).

A link's run-loop task also gets a done-callback so an unexpected death is
logged and the entry dropped — a later refresh then rebuilds it instead of
leaving a permanently dead ``connected=False`` object behind.

Deliberately absent: a missing daemons file does **not** unplug every daemon.
The common causes (typo in the path, unmounted home) look exactly like "the
user removed all daemons", and dropping live links on that basis would strand
running work. Removal has its own explicit path (``agt pairing remove``).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Callable

from ..config import DaemonTarget, ServerConfig, _parse_daemons_file
from ..daemon_link import DaemonLink

log = logging.getLogger("agt.server.daemon_link_pool")

# How long a refresh waits for a re-dial before answering the HTTP caller.
# Long enough that a reachable daemon reports ``connected: true``; short
# enough that clicking a button never hangs the UI.
_REDIAL_WAIT_SECONDS = 1.5


class DaemonLinkPool:
    """The Server's live reverse-dial link set, reloadable at runtime."""

    def __init__(
        self,
        *,
        config: ServerConfig,
        session_factory: Any,
        daemon_defs: dict[str, DaemonTarget],
        shutdown: asyncio.Event,
        initial_targets: list[DaemonTarget] | None = None,
        link_factory: Callable[..., DaemonLink] | None = None,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        # Shared, mutated in place: ``DaemonLink`` and the MCP catalog both
        # hold this dict, so refreshing must update it rather than replace it.
        self._daemon_defs = daemon_defs
        self._shutdown = shutdown
        self._initial_targets = list(
            config.daemons if initial_targets is None else initial_targets
        )
        self._link_factory = link_factory or DaemonLink
        self.links: dict[str, DaemonLink] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    @property
    def daemons_path(self) -> str:
        return self._config.daemons_path or ""

    async def start(self) -> None:
        """Dial the targets the config was built with (no file re-read)."""
        async with self._lock:
            for target in self._initial_targets:
                if target.daemon_id not in self.links:
                    self._start_link(target)

    async def stop(self) -> None:
        """Stop every link and wait for its run loop to exit."""
        async with self._lock:
            for daemon_id in list(self.links):
                await self._stop_link(daemon_id)

    async def refresh(self, *, daemon_id: str | None = None) -> list[dict[str, Any]]:
        """Re-read the daemons file and reconcile the live link set.

        ``daemon_id`` narrows the forced re-dial to one target (the settings
        page's per-row button); registry reconciliation is always global
        because the file is global and the work is cheap.

        Returns one row per configured target, in file order, plus a
        ``removed`` row for every target dropped from the file.
        """
        async with self._lock:
            targets = self._targets_from_file()
            desired = {target.daemon_id: target for target in targets}

            removed: list[dict[str, Any]] = []
            for removed_id in sorted(set(self.links) - set(desired)):
                await self._stop_link(removed_id)
                removed.append(
                    {"daemon_id": removed_id, "action": "removed", "connected": False}
                )

            # Publish the new registry before starting links: ``DaemonLink``
            # keeps a reference to this dict for its catalog overlay.
            self._config.daemons = targets
            self._daemon_defs.clear()
            self._daemon_defs.update(desired)

            report: list[dict[str, Any]] = list(removed)
            redialled: list[DaemonLink] = []
            for target in targets:
                link = self.links.get(target.daemon_id)
                action = "unchanged"
                if link is None:
                    link = self._start_link(target)
                    action = "added"
                elif link.url != target.url:
                    await self._stop_link(target.daemon_id)
                    link = self._start_link(target)
                    action = "replaced"
                elif not link.connected and (
                    daemon_id is None or daemon_id == target.daemon_id
                ):
                    link.kick()
                    redialled.append(link)
                    action = "redialled"
                report.append(
                    {
                        "daemon_id": target.daemon_id,
                        "action": action,
                        "connected": link.connected,
                    }
                )

            if redialled:
                # Give the kick a moment so the response's ``connected`` flag
                # is honest instead of "we just started trying".
                await asyncio.gather(
                    *(link.wait_connected(_REDIAL_WAIT_SECONDS) for link in redialled),
                    return_exceptions=True,
                )
                for row in report:
                    if row["action"] == "redialled":
                        link = self.links.get(row["daemon_id"])
                        row["connected"] = link.connected if link is not None else False

            log.info(
                "Daemon link refresh: %s",
                ", ".join(
                    f"{row['daemon_id']}={row['action']}"
                    f"{'(online)' if row['connected'] else ''}"
                    for row in report
                )
                or "no configured daemons",
            )
            return report

    def _targets_from_file(self) -> list[DaemonTarget]:
        path = self.daemons_path
        if not path:
            return list(self._config.daemons)
        try:
            exists = Path(path).expanduser().exists()
        except OSError:
            exists = False
        if not exists:
            # Keep the current registry — see the module docstring.
            log.warning(
                "Daemon refresh: %s does not exist; keeping %d configured link(s)",
                path,
                len(self._config.daemons),
            )
            return list(self._config.daemons)
        return _parse_daemons_file(path)

    def _start_link(self, target: DaemonTarget) -> DaemonLink:
        link = self._link_factory(
            daemon_id=target.daemon_id,
            url=target.url,
            secret=self._config.secret,
            session_factory=self._session_factory,
            name=target.name,
            db_path=self._config.db_path,
            daemon_defs=self._daemon_defs,
        )
        self.links[target.daemon_id] = link
        task = asyncio.create_task(
            link.run(self._shutdown), name=f"daemon-link:{target.daemon_id}"
        )
        self._tasks[target.daemon_id] = task
        task.add_done_callback(
            lambda done, did=target.daemon_id: self._task_done(did, done)
        )
        return link

    async def _stop_link(self, daemon_id: str) -> None:
        link = self.links.pop(daemon_id, None)
        # Detach the task before stopping so the done-callback below sees a
        # stale entry and stays quiet — a deliberate stop is not a death.
        task = self._tasks.pop(daemon_id, None)
        if link is not None:
            try:
                await link.stop()
            except Exception:  # pragma: no cover — stop is best effort
                log.debug("DaemonLink %s stop failed", daemon_id, exc_info=True)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def _task_done(self, daemon_id: str, task: asyncio.Task[None]) -> None:
        """Log an unexpected run-loop exit and drop the dead entries.

        The ``self._tasks`` guard keeps (a) a link stopped on purpose from
        being reported as a death and (b) a callback from a link that was
        already replaced (URL change) from evicting its replacement.
        """
        if self._tasks.get(daemon_id) is not task:
            return
        self._tasks.pop(daemon_id, None)
        self.links.pop(daemon_id, None)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("DaemonLink %s run loop died: %s", daemon_id, exc)
        elif not self._shutdown.is_set():
            log.warning("DaemonLink %s run loop exited unexpectedly", daemon_id)


__all__ = ["DaemonLinkPool"]
