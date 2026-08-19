"""SessionStore — local task_id ↔ session_id mapping.

The store only holds *pointers*. The full conversation context lives inside
the agent CLI itself (e.g. Claude Code's ~/.claude/projects/<hash>/*.jsonl).
We just remember which session id corresponds to which task so we can ask the
agent to `--resume` it on the next run.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SessionRecord:
    """A pointer mapping: this task previously used this agent session."""

    task_id: str
    provider: str
    session_id: str
    created_at: float


class SessionStore:
    """JSON-file-backed task_id ↔ session_id mapping."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or (Path.home() / ".agt" / "sessions.json")
        self._records: dict[str, SessionRecord] = {}
        self._load()

    @classmethod
    def at(cls, path: Path) -> "SessionStore":
        """Convenience constructor for a per-task store rooted at ``path``.

        Phase 4: when ``task.work_dir`` is set, the daemon writes the
        session pointer there instead of the global ``~/.agt/sessions.json``
        so each task's session state lives next to its other AGT
        metadata and `agt task clean` can remove it cleanly.
        """
        return cls(path=path / "sessions.json")

    # ── Public API ──────────────────────────────────────────────

    def get(self, task_id: str, provider: str) -> str | None:
        """Return the session_id for this task+provider, or None."""
        rec = self._records.get(task_id)
        if rec is not None and rec.provider == provider:
            return rec.session_id
        return None

    def save(self, task_id: str, provider: str, session_id: str) -> None:
        """Record that this task is now bound to this agent session."""
        self._records[task_id] = SessionRecord(
            task_id=task_id,
            provider=provider,
            session_id=session_id,
            created_at=time.time(),
        )
        self._persist()

    def delete(self, task_id: str) -> None:
        """Forget any recorded session for this task. No-op if absent."""
        if self._records.pop(task_id, None) is not None:
            self._persist()

    def has(self, task_id: str) -> bool:
        return task_id in self._records

    # ── Persistence ─────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable file — start fresh rather than crash.
            self._records = {}
            return
        records_raw = data.get("records", {}) if isinstance(data, dict) else {}
        loaded: dict[str, SessionRecord] = {}
        for task_id, rec in records_raw.items():
            if not isinstance(rec, dict):
                continue
            try:
                loaded[task_id] = SessionRecord(
                    task_id=task_id,
                    provider=str(rec.get("provider", "")),
                    session_id=str(rec.get("session_id", "")),
                    created_at=float(rec.get("created_at", 0.0)),
                )
            except (TypeError, ValueError):
                continue
        self._records = loaded

    def _persist(self) -> None:
        payload = {
            "records": {
                task_id: asdict(rec) for task_id, rec in self._records.items()
            }
        }
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            # Persistence failure shouldn't crash the daemon — log later if needed.
            pass


__all__ = ["SessionRecord", "SessionStore"]