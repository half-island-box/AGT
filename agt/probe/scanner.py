"""AgentProbe — scan the local machine for installed AI agent CLIs."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ..runtime.models import AgentEntry
from .known_agents import KnownAgent, get_all_known_agents
from .shell_resolver import find_in_dirs, resolve_path_from_login_shell


_VERSION_TIMEOUT = 5
_VERSION_REGEX = re.compile(r"(\d+\.\d+\.\d+(?:[-+a-zA-Z0-9.]*)?)")


def _lookup_env_override(command: str) -> str | None:
    """Look up AGT_<COMMAND>_PATH override for an agent."""
    env_key = f"AGT_{command.upper().replace('-', '_')}_PATH"
    value = os.environ.get(env_key)
    if not value:
        return None
    path = Path(value)
    if path.is_file() and os.access(path, os.X_OK):
        return str(path)
    return None


def _parse_version(raw: str) -> str:
    """Pull the first X.Y.Z-shaped token out of a version string, or 'unknown'."""
    if not raw:
        return "unknown"
    match = _VERSION_REGEX.search(raw)
    return match.group(1) if match else "unknown"


def _probe_version(binary_path: str) -> str:
    """Run `<binary> --version` and parse the version. Never raises."""
    try:
        proc = subprocess.run(
            [binary_path, "--version"],
            capture_output=True,
            text=True,
            timeout=_VERSION_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "unknown"
    combined = (proc.stdout or "") + (proc.stderr or "")
    return _parse_version(combined)


def _resolve_command_path(command: str) -> str | None:
    """Three-tier lookup: which -> env override -> shell fallback."""
    direct = shutil.which(command)
    if direct:
        return direct

    override = _lookup_env_override(command)
    if override:
        return override

    shell_dirs = resolve_path_from_login_shell()
    if shell_dirs:
        candidate = find_in_dirs(command, shell_dirs)
        if candidate:
            return candidate

    return None

#agent探测器
class AgentProbe:
    """Discovers installed AI agent CLIs on the local machine.

    The probe caches its first scan to avoid repeated fork() of the login
    shell. Use ``force_refresh()`` to clear the cache.
    """

    def __init__(self) -> None:
        self._cache: dict[str, AgentEntry] | None = None

    def scan(self, use_cache: bool = True) -> list[AgentEntry]:
        """Return all discovered AgentEntries. Cached on first call by default."""
        if use_cache and self._cache is not None:
            return list(self._cache.values())

        results: dict[str, AgentEntry] = {}
        for known in get_all_known_agents():
            entry = self._probe_one(known)
            if entry is not None:
                results[known.provider] = entry

        self._cache = results
        return list(results.values())

    def force_refresh(self) -> list[AgentEntry]:
        """Drop the cache and re-scan from scratch."""
        self._cache = None
        return self.scan(use_cache=False)

    def _probe_one(self, known: KnownAgent) -> AgentEntry | None:
        path = _resolve_command_path(known.command)
        if path is None:
            return None
        version = _probe_version(path)
        return AgentEntry(
            command=known.command,
            path=Path(path),
            version=version,
            provider=known.provider,
            display_name=known.display_name,
            cli_style=known.cli_style,
        )


__all__ = ["AgentProbe"]