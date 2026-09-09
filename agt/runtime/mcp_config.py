"""Provider MCP-config injection profiles (Phase B).

The live runtime catalog is served over MCP. To let a spawned agent
*discover* that MCP server, the executor injects a provider-specific
config file into the spawn environment before spawn, then reclaims it in
``finally``. Providers read MCP config from different places and formats,
so the injection is modelled as a small strategy per provider:

- :class:`ProjectJsonMcpConfig` — a project-scoped JSON file
  (``{"mcpServers": {...}}``), e.g. Claude Code's ``.mcp.json``. The file
  lives in the spawn cwd, so the inject/reclaim contract can back up the
  original bytes and restore them.

- :class:`GlobalTomlMcpConfig` — a *global* TOML file (Codex's
  ``~/.codex/config.toml``). Because the file is shared and not
  project-scoped, it must never be whole-file backed-up or restored: the
  profile adds/removes only its own ``[mcp_servers.<name>]`` table, under
  a lock, preserving the user's other keys and formatting.

- ``None`` (the ``ProviderAdapter.mcp_profile`` value for unsupported
  providers): the :func:`inject_mcp_config` / :func:`reclaim_mcp_config`
  dispatchers below no-op.

Both concrete profiles carry the same two-method surface
(:class:`McpConfigProfile`) so the executor calls them uniformly without
knowing which format a provider uses.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# The MCP server name an agent references when it calls the catalog tool.
# Both profiles write (or would write) an entry under this name.
MCP_SERVER_NAME = "agt-catalog"

# Sidecar prefix (project-scoped JSON profile) under ``<cwd>/.agt/``.
_SIDECAR_PREFIX = ".agt-mcp"


class McpConfigProfile(Protocol):
    """Per-provider MCP config injection strategy.

    The executor calls :meth:`inject` before spawn and :meth:`reclaim` in
    its ``finally``. Implementations own every provider-specific detail —
    file location, format, and how much is safe to restore — so the
    executor never branches on provider name or config format.
    """

    def inject(self, cwd: Path, mcp_url: str) -> None: ...

    def reclaim(self, cwd: Path) -> None: ...


# ── Project-scoped JSON (Claude Code ``.mcp.json``) ─────────────────────


@dataclass(frozen=True)
class ProjectJsonMcpConfig:
    """Register ``agt-catalog`` in a project-scoped JSON MCP config.

    Non-destructive: a pre-existing file is merged (never clobbered) and
    its original bytes restored on reclaim; only a file we created
    ourselves (tracked by a ``managed`` sidecar) is deleted on reclaim.
    Idempotent (reclaim-first, so a leftover from a crashed prior run is
    cleaned before re-injecting). Pure file IO under ``<cwd>/.agt/``.
    """

    filename: str = ".mcp.json"

    def _sidecars(self, cwd: Path) -> tuple[Path, Path]:
        aagt = cwd / ".agt"
        return (
            aagt / f"{_SIDECAR_PREFIX}-backup-{self.filename}",
            aagt / f"{_SIDECAR_PREFIX}-managed-{self.filename}",
        )

    def inject(self, cwd: Path, mcp_url: str) -> None:
        cwd = Path(cwd)
        cwd.mkdir(parents=True, exist_ok=True)
        # Crash-self-heal: strip a leftover from a prior run before
        # re-injecting.
        self.reclaim(cwd)

        target = cwd / self.filename
        aagt = cwd / ".agt"
        aagt.mkdir(parents=True, exist_ok=True)
        backup, managed = self._sidecars(cwd)

        # Read any pre-existing file, tolerating a TOCTOU race (the file
        # vanishes between ``exists()`` and the read). ``raw is None`` means
        # "no original to preserve" — either the file never existed or it
        # disappeared mid-read, and we treat it as ours.
        original: dict = {}
        raw: bytes | None = None
        try:
            if target.exists():
                raw = target.read_bytes()
        except OSError:
            raw = None

        if raw is not None:
            # Preserve the user's bytes verbatim; merge on top of a parsed copy.
            backup.write_bytes(raw)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                parsed = {}
            if isinstance(parsed, dict):
                original = parsed
        else:
            managed.touch()

        servers = dict(original.get("mcpServers") or {})
        servers[MCP_SERVER_NAME] = {"type": "http", "url": mcp_url}
        merged = dict(original)
        merged["mcpServers"] = servers
        target.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")

    def reclaim(self, cwd: Path) -> None:
        """Undo a prior :meth:`inject`. Idempotent.

        - If we created the file (``managed`` sidecar present) → delete it.
        - Else if a backup exists (the file pre-existed) → restore the
          original bytes.
        - Otherwise → nothing was injected, nothing to do.
        """
        cwd = Path(cwd)
        target = cwd / self.filename
        backup, managed = self._sidecars(cwd)

        if managed.exists():
            with contextlib.suppress(OSError):
                target.unlink()
            with contextlib.suppress(OSError):
                managed.unlink()
            with contextlib.suppress(OSError):
                backup.unlink()
            return

        if backup.exists():
            try:
                target.write_bytes(backup.read_bytes())
            except OSError:
                pass
            with contextlib.suppress(OSError):
                backup.unlink()


# ── Global TOML (Codex ``~/.codex/config.toml``) ────────────────────────


def _strip_table(text: str, header: str) -> str:
    """Remove the block beginning with ``header`` from TOML ``text``.

    The block is ``header`` plus every following line up to (but excluding)
    the next non-indented ``[`` line (the next table header). Returns
    ``text`` unchanged when ``header`` is absent, so a no-op reclaim never
    rewrites the user's file and preserves bytes it has no reason to touch.
    """
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    found = False
    while i < n:
        line = lines[i]
        if line.strip() == header:
            found = True
            i += 1
            while i < n:
                body = lines[i]
                stripped = body.strip()
                if stripped.startswith("[") and not body.startswith((" ", "\t")):
                    break
                i += 1
            continue
        out.append(line)
        i += 1
    if not found:
        return text
    return "\n".join(out).rstrip() + "\n"


@dataclass(frozen=True)
class GlobalTomlMcpConfig:
    """Register ``agt-catalog`` in a *global* TOML MCP config (Codex).

    The target (default ``~/.codex/config.toml``) is shared across
    sessions, so this profile is deliberately minimal-footprint:

    - **Section-scoped** — only the ``[<table>.<name>]`` table is added on
      inject and removed on reclaim; the user's other keys/tables (and
      their formatting) are left byte-for-byte intact. Never whole-file
      backup/restore.
    - **Locked** — each read-modify-write is guarded by an ``fcntl`` lock
      (lockfile under ``state_dir``) so concurrent spawns on one machine
      can't corrupt each other's edits.
    - **Leaves no trace** — if the file did not exist before inject, reclaim
      deletes the now-empty file and its ``managed`` marker.

    Codex's Streamable-HTTP transport also needs the global
    ``experimental_use_rmcp_client = true`` flag; this profile deliberately
    does **not** write it (it is a user-level policy change, not a per-task
    injection). Users enable it once themselves.
    """

    path: Path = Path.home() / ".codex" / "config.toml"
    table: str = "mcp_servers"
    name: str = MCP_SERVER_NAME
    state_dir: Path = field(default_factory=lambda: Path.home() / ".agt" / "mcp")

    @property
    def _header(self) -> str:
        return f"[{self.table}.{self.name}]"

    def _key(self) -> str:
        digest = hashlib.sha1(str(self.path).encode("utf-8")).hexdigest()[:12]
        return f"{self.path.name}.{digest}"

    def _marker(self) -> Path:
        return self.state_dir / f"{self._key()}.managed"

    def _lock_path(self) -> Path:
        return self.state_dir / f"{self._key()}.lock"

    @contextlib.contextmanager
    def _locked(self):
        import fcntl  # POSIX-only; the daemon runtime is Linux/macOS.

        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock_path(), "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _read_text(self) -> str | None:
        try:
            return self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return None

    def _block(self, mcp_url: str) -> str:
        # ``json.dumps`` produces a double-quoted, correctly-escaped string
        # that is also a valid TOML basic string for our URL values.
        return f"{self._header}\nurl = {json.dumps(mcp_url)}\n"

    def inject(self, cwd: Path, mcp_url: str) -> None:
        with self._locked():
            self._inject_locked(mcp_url)

    def _inject_locked(self, mcp_url: str) -> None:
        existed = self.path.exists()
        text = self._read_text() or ""
        # Idempotent: drop any prior copy of our table before appending.
        stripped = _strip_table(text, self._header).rstrip()
        block = self._block(mcp_url)
        new_text = f"{stripped}\n\n{block}" if stripped else block
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(new_text, encoding="utf-8")
        if not existed:
            self._marker().touch()

    def reclaim(self, cwd: Path) -> None:
        with self._locked():
            self._reclaim_locked()

    def _reclaim_locked(self) -> None:
        marker = self._marker()
        created = marker.exists()
        text = self._read_text()
        if text is None:
            # Nothing on disk to clean; just clear a stale marker.
            with contextlib.suppress(OSError):
                marker.unlink()
            return

        stripped = _strip_table(text, self._header)
        if not stripped.strip() and created:
            with contextlib.suppress(OSError):
                self.path.unlink()
        elif stripped != text:
            self.path.write_text(stripped, encoding="utf-8")
        with contextlib.suppress(OSError):
            marker.unlink()


# ── Dispatchers (executor-facing surface) ───────────────────────────────


def inject_mcp_config(
    profile: McpConfigProfile | None, cwd: Path, mcp_url: str
) -> None:
    """Register the catalog MCP for ``profile``; no-op when ``None``."""
    if profile is not None:
        profile.inject(cwd, mcp_url)


def reclaim_mcp_config(profile: McpConfigProfile | None, cwd: Path) -> None:
    """Undo a prior :func:`inject_mcp_config`; no-op when ``None``."""
    if profile is not None:
        profile.reclaim(cwd)


__all__ = [
    "MCP_SERVER_NAME",
    "McpConfigProfile",
    "ProjectJsonMcpConfig",
    "GlobalTomlMcpConfig",
    "inject_mcp_config",
    "reclaim_mcp_config",
]