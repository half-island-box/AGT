"""TaskPreparer — pre-execution environment preparation.

Handles:
  * Git clone of a task's target repo into the task work_dir.
  * Branch checkout when a non-default branch is requested.
  * Skill bundle download (best-effort; skipped if URL is missing).
  * Auth token injection into the agent subprocess environment.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("agt.task.prepare")


@dataclass
class PreparedTask:
    """The prepared environment for an agent subprocess."""

    work_dir: Path
    extra_env: dict[str, str] = field(default_factory=dict)
    bundle_dir: Path | None = None


@dataclass
class TaskPrepareSpec:
    """Inputs that may arrive via Server task payload."""

    repo_url: str | None = None
    branch: str = "main"
    skill_bundle_url: str | None = None
    auth_token: str | None = None


class TaskPreparer:
    """Prepare a task's environment before spawning the agent subprocess."""

    def __init__(self, work_dir_root: Path) -> None:
        self._work_dir_root = work_dir_root

    async def prepare(
        self,
        task_id: str,
        provider: str,
        spec: TaskPrepareSpec,
    ) -> PreparedTask:
        work_dir = self._work_dir_root / provider / task_id
        work_dir.mkdir(parents=True, exist_ok=True)

        bundle_dir: Path | None = None
        if spec.repo_url:
            await self._clone(spec.repo_url, spec.branch, work_dir)
        if spec.skill_bundle_url:
            bundle_dir = work_dir / ".skills"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            await self._download(spec.skill_bundle_url, bundle_dir)
        extra_env: dict[str, str] = {}
        if spec.auth_token:
            extra_env["AGT_TASK_TOKEN"] = spec.auth_token

        return PreparedTask(work_dir=work_dir, extra_env=extra_env, bundle_dir=bundle_dir)

    async def _clone(self, repo_url: str, branch: str, work_dir: Path) -> None:
        """Run ``git clone`` into work_dir. No-op if dir already has a repo."""
        if (work_dir / ".git").exists():
            log.info("Repo already cloned at %s; switching branch", work_dir)
            await asyncio.create_subprocess_exec(
                "git", "-C", str(work_dir), "checkout", branch
            )
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "clone",
                "--branch",
                branch,
                "--single-branch",
                repo_url,
                str(work_dir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        except (FileNotFoundError, asyncio.TimeoutError) as exc:
            log.warning("git clone skipped: %s", exc)
            return
        if proc.returncode != 0:
            log.warning(
                "git clone failed: %s", stderr.decode(errors="replace")[:200]
            )

    async def _download(self, url: str, dest: Path) -> None:
        """Best-effort skill bundle download. Skips on any error."""
        try:
            # Run the synchronous urllib call in a thread so we don't block.
            def _fetch() -> None:
                with urllib.request.urlopen(url, timeout=30) as resp:
                    with (dest / "bundle.tar").open("wb") as out:
                        shutil.copyfileobj(resp, out)
            await asyncio.to_thread(_fetch)
            log.info("Downloaded skill bundle to %s", dest)
        except Exception as exc:
            log.warning("skill bundle download failed: %s", exc)


__all__ = ["TaskPreparer", "TaskPrepareSpec", "PreparedTask"]