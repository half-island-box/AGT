"""Locate, check, and build the browser-console bundles.

The console pages are esbuild bundles produced by ``npm run build:console``
in the ``vscode/`` workspace and written into this package's ``assets/``
directory. Release builds ship those files inside the wheel, so an installed
``agt`` never needs Node.

A source checkout is different: the bundles are versioned alongside the
webview sources, and a branch switch (or a fresh clone that has not built
yet) leaves the package without them. :func:`ensure_console_assets` lets
``agt server start`` notice that and run the npm script itself once, so
starting the server stays a single command.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("agt.web_console")

# One bundle per console page served by ``agt/server/console.py``.
CONSOLE_PAGES: tuple[str, ...] = ("projects", "chat", "sop", "goal", "settings")

_BUILD_SCRIPT = "build:console"
# A lock older than this is treated as abandoned (the holder crashed).
_LOCK_STALE_SECONDS = 900.0
_DEFAULT_TIMEOUT_SECONDS = 300.0


def default_assets_dir() -> Path:
    """The assets directory that ships inside this package."""
    return Path(__file__).resolve().parent / "assets"


def bundle_names() -> list[str]:
    """Every JavaScript bundle the console needs, one per page."""
    return [f"webview-console-{page}.js" for page in CONSOLE_PAGES]


def missing_bundles(assets_dir: Path | str) -> list[str]:
    """Bundles absent from ``assets_dir``; empty means the console can serve."""
    root = Path(assets_dir).expanduser()
    return [name for name in bundle_names() if not (root / name).is_file()]


def _is_console_workspace(workspace: Path) -> bool:
    """True when ``workspace`` is the ``vscode/`` tree that builds the console."""
    package_json = workspace / "package.json"
    try:
        data = json.loads(package_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    scripts = data.get("scripts")
    return isinstance(scripts, dict) and _BUILD_SCRIPT in scripts


def console_source_dir(start: Path | str | None = None) -> Path | None:
    """The workspace that can rebuild the bundles, if one is reachable.

    ``AGT_CONSOLE_SOURCE_DIR`` points at it explicitly (useful when the
    package is installed from a checkout that lives elsewhere). Otherwise
    the tree containing this module is searched upwards for ``vscode/``.
    """
    override = os.environ.get("AGT_CONSOLE_SOURCE_DIR", "").strip()
    if override:
        candidate = Path(override).expanduser()
        return candidate if _is_console_workspace(candidate) else None
    base = Path(start).expanduser() if start is not None else Path(__file__).resolve()
    for directory in [*base.parents, base] if base.is_file() else [base, *base.parents]:
        workspace = directory / "vscode"
        if _is_console_workspace(workspace):
            return workspace
    return None


@dataclass(frozen=True)
class EnsureResult:
    """Outcome of one :func:`ensure_console_assets` call.

    ``status`` is one of ``present`` (nothing to do), ``built`` (the npm
    script ran and produced every bundle), ``no-source`` / ``no-npm`` /
    ``disabled`` (cannot build here), ``locked`` (another process is
    building), or ``build-failed``.
    """

    status: str
    detail: str = ""
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status in {"present", "built"}


def _tail(text: str, limit: int = 400) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"…{text[-limit:]}"


def _lock_path(assets_dir: Path) -> Path:
    digest = hashlib.sha1(str(assets_dir.resolve()).encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"agt-console-build-{digest}.lock"


def _acquire_lock(assets_dir: Path) -> Path | None:
    """Create the build lock, or None when someone else holds it."""
    path = _lock_path(assets_dir)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _LOCK_STALE_SECONDS:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            return None
        except OSError:
            return None
        try:
            os.write(fd, str(os.getpid()).encode())
        finally:
            os.close(fd)
        return path
    return None


def _release_lock(path: Path | None) -> None:
    if path is not None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_console_assets(
    assets_dir: Path | str,
    *,
    source_dir: Path | str | None = None,
    npm: str | None = None,
    runner: Callable[..., Any] | None = None,
    timeout: float = _DEFAULT_TIMEOUT_SECONDS,
) -> EnsureResult:
    """Make ``assets_dir`` servable, building it when this is a source checkout.

    Never raises: every failure is reported in the returned status, so the
    caller can warn and let the Server start anyway (the pages then answer
    503 with the same hint).
    """
    root = Path(assets_dir).expanduser()
    if not missing_bundles(root):
        return EnsureResult("present")
    if os.environ.get("AGT_CONSOLE_AUTOBUILD", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return EnsureResult("disabled")
    workspace = Path(source_dir).expanduser() if source_dir is not None else console_source_dir()
    if workspace is None or not _is_console_workspace(workspace):
        return EnsureResult("no-source")
    npm_bin = npm if npm is not None else shutil.which("npm")
    if not npm_bin:
        return EnsureResult("no-npm")

    lock = _acquire_lock(root)
    if lock is None:
        return EnsureResult("locked")
    run: Callable[..., Any] = runner or subprocess.run
    started = time.monotonic()
    try:
        # Another process may have finished between our check and the lock.
        if not missing_bundles(root):
            return EnsureResult("present")
        try:
            completed = run(
                [npm_bin, "run", _BUILD_SCRIPT],
                cwd=str(workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return EnsureResult("build-failed", f"timed out after {timeout:.0f}s")
        except OSError as exc:
            return EnsureResult("build-failed", str(exc))
    finally:
        _release_lock(lock)

    seconds = time.monotonic() - started
    output = f"{getattr(completed, 'stdout', '') or ''}{getattr(completed, 'stderr', '') or ''}"
    if getattr(completed, "returncode", 1) != 0:
        return EnsureResult("build-failed", _tail(output), seconds)
    missing = missing_bundles(root)
    if missing:
        return EnsureResult(
            "build-failed",
            f"npm finished but {', '.join(missing)} is still missing",
            seconds,
        )
    return EnsureResult("built", _tail(output), seconds)


__all__ = [
    "CONSOLE_PAGES",
    "EnsureResult",
    "bundle_names",
    "console_source_dir",
    "default_assets_dir",
    "ensure_console_assets",
    "missing_bundles",
]
