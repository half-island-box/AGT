"""Resolve PATH via the user's login shell.

Used as a fallback when a CLI is not on the current process PATH — common when:
- The terminal was launched from a GUI app and never inherited login shell env.
- Tools are managed by nvm / fnm / volta / asdf which extend PATH via .bashrc / .zshrc.
- Homebrew on Apple Silicon installs to /opt/homebrew/bin.
"""

from __future__ import annotations

import os
import shlex
import subprocess

PATH_START_MARKER = "__AGT_PATH_START__"
PATH_END_MARKER = "__AGT_PATH_END__"
SHELL_TIMEOUT_SECONDS = 5


def _clean_env() -> dict[str, str]:
    """Build a minimal env for the login shell, so current PATH doesn't leak in."""
    keep = {"HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE"}
    return {k: v for k, v in os.environ.items() if k in keep}


def _shell_command(shell: str) -> list[str]:
    """Build the command to print the login shell's PATH wrapped in markers."""
    # Use a single-quoted heredoc-style print so .bashrc noise is bracketed.
    echo = (
        f"printf '%s\\n' '{PATH_START_MARKER}:$PATH:{PATH_END_MARKER}'"
    )
    return [shell, "-l", "-c", echo]


def resolve_path_from_login_shell(shell: str | None = None) -> list[str] | None:
    """Return directories in PATH as resolved by the user's login shell.

    Returns None on any failure (timeout, missing shell, parse error).
    """
    shell = shell or os.environ.get("SHELL") or "/bin/sh"
    try:
        proc = subprocess.run(
            _shell_command(shell),
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
            env=_clean_env(),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None

    output = proc.stdout
    start = output.find(PATH_START_MARKER)
    end = output.find(PATH_END_MARKER)
    if start == -1 or end == -1 or end <= start:
        return None

    # Strip marker + leading ':' that follows the start marker.
    payload = output[start + len(PATH_START_MARKER) : end]
    if payload.startswith(":"):
        payload = payload[1:]
    if payload.endswith(":"):
        payload = payload[:-1]

    return [d for d in payload.split(":") if d]


def find_in_dirs(command: str, dirs: list[str]) -> str | None:
    """Return the first matching executable path found in any directory, or None."""
    for directory in dirs:
        candidate = os.path.join(directory, command)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


__all__ = [
    "PATH_START_MARKER",
    "PATH_END_MARKER",
    "SHELL_TIMEOUT_SECONDS",
    "resolve_path_from_login_shell",
    "find_in_dirs",
]