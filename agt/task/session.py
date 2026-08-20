"""CwdSessionStore — cwd-scoped session index for ``agt chat``.

Phase 4 chat-multisession: instead of one ``sessions.json`` per task
(PK = task_id), we keep one file **per cwd**. Each chat session inside
that cwd is its own entry — multiple sessions coexist, none overwrite
each other, and ``agt chat --list`` / ``--resume <sid>`` can address
any of them by ``session_id``.

File layout:

```
<cwd>/.agt/sessions.json
{
  "cwd": "/home/user/project-x",
  "provider": "claude",
  "sessions": [
    {
      "session_id": "abc-1234…",
      "provider": "claude",
      "first_seen_at": 1724000000.0,
      "last_seen_at":  1724001000.0,
      "task_id": "chat-x1y2z3w4",
      "turn_count": 5,
      "first_prompt": "explain this module"
    },
    ...
  ]
}
```

Each entry carries its own ``provider`` — one cwd can hold sessions from
several agents (e.g. a claude chat and a codex chat), and ``--list``
shows/filters by that field. The top-level ``provider`` key is kept only
as a legacy default for entries written before per-entry providers
existed.

Ordering invariant: ``sessions`` is kept sorted by ``last_seen_at`` desc,
so ``--list`` shows most-recent first without sorting on every read.

Legacy migration: pre-multisession files used ``{"records": {task_id:
{...}}}``. On first load, we detect that shape and migrate in place,
preserving ``task_id`` as the entry's ``task_id`` field (with empty
``first_prompt`` since we didn't track that). A ``.bak`` snapshot is
written next to the file before migration; ``cleanup_legacy_backup()``
removes it once the user has confirmed the new file works.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class SessionEntry:
    """One chat session inside a cwd."""

    session_id: str
    first_seen_at: float
    last_seen_at: float
    task_id: str
    turn_count: int
    first_prompt: str = ""
    provider: str = "claude"

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class CwdSessionStore:
    """Append-only session index scoped to one cwd.

    Holds entries in memory and persists to ``<cwd>/.agt/sessions.json``
    on every mutation. Designed for small N (single-user, per-project);
    not indexed — linear scans are fine for dozens of sessions.
    """

    cwd: Path
    provider: str = "claude"
    sessions: list[SessionEntry] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.cwd / ".agt" / "sessions.json"

    # ── Construction ────────────────────────────────────────────

    @classmethod
    def load(cls, cwd: Path, *, provider: str = "claude") -> "CwdSessionStore":
        """Load the store from disk, migrating legacy format if needed."""
        store = cls(cwd=cwd, provider=provider)
        store._load()
        return store

    @classmethod
    def load_or_create(cls, cwd: Path, *, provider: str = "claude") -> "CwdSessionStore":
        """Like ``load`` but tolerates a missing / unreadable file by
        returning an empty store. Used by the CLI on first invocation."""
        try:
            return cls.load(cwd, provider=provider)
        except (OSError, json.JSONDecodeError):
            return cls(cwd=cwd, provider=provider)

    # ── Mutation ────────────────────────────────────────────────

    def record(
        self,
        *,
        session_id: str,
        task_id: str,
        first_prompt: str,
        turn_count: int,
    ) -> None:
        """Add a new entry or update an existing one matched by
        ``session_id``. Bumps ``last_seen_at`` and ``turn_count``;
        preserves ``first_seen_at``, the original ``first_prompt``, and
        the entry's ``provider`` (a session's provider never changes).

        New entries are stamped with the store's ``provider`` — the
        provider this store was opened for (see ``load_or_create``).
        """
        now = time.time()
        existing = self._find(session_id)
        if existing is not None:
            existing.last_seen_at = now
            existing.turn_count = turn_count
            # task_id can change across resumes — track the latest
            existing.task_id = task_id
        else:
            self.sessions.append(
                SessionEntry(
                    session_id=session_id,
                    first_seen_at=now,
                    last_seen_at=now,
                    task_id=task_id,
                    turn_count=turn_count,
                    first_prompt=first_prompt,
                    provider=self.provider,
                )
            )
        self._sort_in_place()
        self._persist()

    def delete(self, session_id: str) -> bool:
        """Remove an entry. Returns True if something was removed."""
        before = len(self.sessions)
        self.sessions = [e for e in self.sessions if e.session_id != session_id]
        if len(self.sessions) < before:
            self._persist()
            return True
        return False

    # ── Read ────────────────────────────────────────────────────

    def list_sessions(self) -> list[SessionEntry]:
        """Return all entries, newest first."""
        return list(self.sessions)

    def get(self, session_id: str) -> SessionEntry | None:
        return self._find(session_id)

    def last(self) -> SessionEntry | None:
        """Return the most-recently-used entry, or None."""
        return self.sessions[0] if self.sessions else None

    def is_empty(self) -> bool:
        return not self.sessions

    # ── Persistence ─────────────────────────────────────────────

    def _persist(self) -> None:
        """Atomic-ish write: write to ``.tmp`` then rename. Best-effort
        on permission errors — we never want a store write to crash a
        running chat."""
        target = self.path
        payload = {
            "cwd": str(self.cwd),
            "provider": self.provider,
            "sessions": [e.as_dict() for e in self.sessions],
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(target)
        except OSError:
            # Persistence is best-effort — keep the in-memory state so
            # the current run still works; the user gets a clear error
            # only if a subsequent read fails.
            pass

    def _load(self) -> None:
        target = self.path
        if not target.exists():
            return
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self._ingest(raw)

    def _ingest(self, raw: object) -> None:
        """Parse either the new array format or the legacy dict format."""
        if isinstance(raw, list):
            # Rare: a partial dump. Treat each item as an entry.
            self.sessions = [
                _entry_from_dict(d, default_provider=self.provider)
                for d in raw if isinstance(d, dict)
            ]
        elif isinstance(raw, dict):
            # The top-level ``provider`` is only a legacy default for
            # entries written before per-entry providers existed. It is
            # NOT copied onto ``self.provider`` — that stays the provider
            # this store was opened for, so new records are stamped with
            # the provider actually running now.
            file_provider = raw.get("provider")
            default_provider = (
                file_provider
                if isinstance(file_provider, str) and file_provider
                else self.provider
            )
            legacy_migrated = self._migrate_legacy(raw)
            if legacy_migrated is not None:
                self.sessions = legacy_migrated
                # Persist the migrated shape so legacy state doesn't
                # get re-migrated on every load.
                self._persist()
                return
            sessions_raw = raw.get("sessions", [])
            if isinstance(sessions_raw, list):
                self.sessions = [
                    _entry_from_dict(d, default_provider=default_provider)
                    for d in sessions_raw if isinstance(d, dict)
                ]
        # Sort after load so callers see newest-first regardless of
        # the order the file happened to be written in.
        self._sort_in_place()

    def _migrate_legacy(self, raw: dict) -> list[SessionEntry] | None:
        """Detect the legacy ``{"records": {task_id: {...}}}`` shape and
        convert it to a list of ``SessionEntry``. Returns ``None`` if
        the input isn't legacy (so the caller falls through to the
        new-format parser)."""
        records = raw.get("records")
        if not isinstance(records, dict):
            return None
        migrated: list[SessionEntry] = []
        for task_id, rec in records.items():
            if not isinstance(rec, dict):
                continue
            session_id = rec.get("session_id")
            if not isinstance(session_id, str) or not session_id:
                continue
            provider = str(rec.get("provider") or self.provider)
            created = rec.get("created_at")
            first_seen = float(created) if isinstance(created, (int, float)) else time.time()
            migrated.append(
                SessionEntry(
                    session_id=session_id,
                    provider=provider,
                    first_seen_at=first_seen,
                    last_seen_at=first_seen,  # we don't know better
                    task_id=str(task_id),
                    turn_count=0,  # we didn't track turns
                    first_prompt="",
                )
            )
        # Write a .bak snapshot of the legacy file before we overwrite
        # it on _persist(). Best-effort.
        try:
            self.path.with_suffix(".json.bak").write_text(
                json.dumps(raw, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
        return migrated

    def cleanup_legacy_backup(self) -> bool:
        """Remove the ``.bak`` snapshot left by the legacy migration.

        Call after the user has confirmed the new shape works (e.g.
        end of a successful ``chat`` session). Returns True if a
        backup was removed.
        """
        backup = self.path.with_suffix(".json.bak")
        try:
            backup.unlink()
            return True
        except FileNotFoundError:
            return False

    # ── Internal ────────────────────────────────────────────────

    def _find(self, session_id: str) -> SessionEntry | None:
        for entry in self.sessions:
            if entry.session_id == session_id:
                return entry
        return None

    def _sort_in_place(self) -> None:
        self.sessions.sort(key=lambda e: e.last_seen_at, reverse=True)


def _entry_from_dict(d: dict, *, default_provider: str = "claude") -> SessionEntry:
    """Build a ``SessionEntry`` from a loose dict; tolerant of missing
    fields because files might be hand-edited.

    ``default_provider`` fills in entries written before per-entry
    providers existed (it's the file's top-level ``provider`` value).
    """
    return SessionEntry(
        session_id=str(d.get("session_id", "")),
        provider=str(d.get("provider") or default_provider),
        first_seen_at=float(d.get("first_seen_at", 0.0) or 0.0),
        last_seen_at=float(d.get("last_seen_at", 0.0) or 0.0),
        task_id=str(d.get("task_id", "")),
        turn_count=int(d.get("turn_count", 0) or 0),
        first_prompt=str(d.get("first_prompt", "") or ""),
    )


__all__ = ["CwdSessionStore", "SessionEntry"]