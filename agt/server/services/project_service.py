"""Persistent browser-console projects and safe directory browsing."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import ServerConfig
from ..models import ConsoleProjectModel, JobModel

_MAX_DIRECTORY_ENTRIES = 1000
_RUN_MODES = ("chat", "sop", "goal")


class ProjectServiceError(ValueError):
    """A project/path error carrying an HTTP status code."""

    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message)


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _allowed_roots(config: ServerConfig) -> list[Path]:
    raw = config.console_workspace_roots or [str(Path.home())]
    return [Path(item).expanduser().resolve() for item in raw]


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_project_path(config: ServerConfig, raw: str) -> Path:
    """Canonicalize a user-supplied path and enforce the console roots."""
    value = (raw or "").strip()
    if not value:
        raise ProjectServiceError("path_required", "project path is required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise ProjectServiceError(
            "relative_path", "project path must be absolute"
        )
    try:
        resolved = candidate.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ProjectServiceError(
            "invalid_path", f"cannot resolve project path: {exc}"
        ) from None
    if len(str(resolved)) > 1024:
        raise ProjectServiceError(
            "path_too_long", "project path must be at most 1024 characters"
        )
    if not any(_is_relative_to(resolved, root) for root in _allowed_roots(config)):
        raise ProjectServiceError(
            "path_outside_roots",
            "project path is outside configured console workspace roots",
            status_code=403,
        )
    return resolved


def _clean_name(value: str | None, fallback: str) -> str:
    name = (value or "").strip() or fallback or "Project"
    if len(name) > 128:
        raise ProjectServiceError(
            "invalid_name", "project name must be at most 128 characters"
        )
    if any(char in name for char in "\r\n\t"):
        raise ProjectServiceError(
            "invalid_name", "project name cannot contain control characters"
        )
    return name


def _is_available(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


async def _run_counts(
    db: AsyncSession, paths: list[str]
) -> dict[str, dict[str, int]]:
    if not paths:
        return {}
    rows = (
        await db.execute(
            select(
                JobModel.cwd,
                JobModel.job_type,
                func.count(JobModel.job_id),
            )
            .where(JobModel.cwd.in_(paths))
            .where(JobModel.job_type.in_(_RUN_MODES))
            .group_by(JobModel.cwd, JobModel.job_type)
        )
    ).all()
    result = {path: {mode: 0 for mode in _RUN_MODES} for path in paths}
    for cwd, mode, count in rows:
        result[cwd][mode] = int(count)
    return result


def _counts_for(
    counts: dict[str, dict[str, int]],
    project: ConsoleProjectModel,
) -> dict[str, int] | None:
    """Sum run counts recorded under the project's local and remote paths.

    Chat runs created before the console sent an explicit run-history ``cwd``
    recorded the remote working directory instead of the project path, so a
    project's history can sit under both keys. Returns ``None`` when neither
    path has any run, letting ``_project_dict`` fall back to zeros.
    """
    paths = [project.path]
    if project.remote_path:
        paths.append(project.remote_path)
    merged = {mode: 0 for mode in _RUN_MODES}
    seen = False
    for path in paths:
        value = counts.get(path)
        if value is None:
            continue
        seen = True
        for mode in _RUN_MODES:
            merged[mode] += value.get(mode, 0)
    return merged if seen else None


def _project_dict(
    project: ConsoleProjectModel,
    counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "path": project.path,
        "available": _is_available(Path(project.path)),
        "remote_daemon_id": project.remote_daemon_id,
        "remote_path": project.remote_path,
        "remote": bool(project.remote_daemon_id and project.remote_path),
        "created_at": project.created_at,
        "updated_at": project.updated_at,
        "last_opened_at": project.last_opened_at,
        "run_counts": counts or {mode: 0 for mode in _RUN_MODES},
    }


async def list_projects(
    db: AsyncSession, config: ServerConfig
) -> list[dict[str, Any]]:
    rows = (await db.execute(select(ConsoleProjectModel))).scalars().all()
    paths = [path for row in rows for path in (row.path, row.remote_path) if path]
    counts = await _run_counts(db, paths)
    projects = [_project_dict(row, _counts_for(counts, row)) for row in rows]
    projects.sort(
        key=lambda item: (
            item["last_opened_at"] or item["updated_at"],
            item["id"],
        ),
        reverse=True,
    )
    return projects


async def get_project(
    db: AsyncSession, project_id: str
) -> dict[str, Any] | None:
    project = await db.get(ConsoleProjectModel, project_id)
    if project is None:
        return None
    paths = [path for path in (project.path, project.remote_path) if path]
    counts = await _run_counts(db, paths)
    return _project_dict(project, _counts_for(counts, project))


def _clean_remote_path(value: str | None) -> str | None:
    """Normalize an optional remote working directory.

    The path belongs to another machine, so it is *not* resolved or checked
    against this Server's filesystem. Structural checks only — absolute,
    bounded length; existence/allowlist enforcement happens on the daemon.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    if not raw.startswith("/") and not (len(raw) > 2 and raw[1] == ":"):
        raise ProjectServiceError(
            "remote_path_not_absolute",
            "remote working directory must be an absolute path",
        )
    if len(raw) > 1024:
        raise ProjectServiceError(
            "remote_path_too_long",
            "remote working directory must be at most 1024 characters",
        )
    return raw


async def create_project(
    db: AsyncSession,
    config: ServerConfig,
    *,
    raw_path: str,
    raw_name: str | None,
    remote_daemon_id: str | None = None,
    raw_remote_path: str | None = None,
) -> dict[str, Any]:
    path = resolve_project_path(config, raw_path)
    if not _is_available(path):
        raise ProjectServiceError(
            "path_not_directory", "project path must be an existing directory"
        )

    remote_daemon = (remote_daemon_id or "").strip() or None
    remote_path = _clean_remote_path(raw_remote_path)
    if (remote_daemon is None) != (remote_path is None):
        raise ProjectServiceError(
            "remote_pair_incomplete",
            "remote_daemon_id and remote_path must be provided together",
        )

    duplicate = await db.execute(
        select(ConsoleProjectModel).where(ConsoleProjectModel.path == str(path))
    )
    if duplicate.scalar_one_or_none() is not None:
        raise ProjectServiceError(
            "project_already_exists",
            f"a project already exists for {path}",
            status_code=409,
        )

    if remote_daemon is not None and remote_path is not None:
        remote_duplicate = await db.execute(
            select(ConsoleProjectModel).where(
                ConsoleProjectModel.remote_daemon_id == remote_daemon,
                ConsoleProjectModel.remote_path == remote_path,
            )
        )
        if remote_duplicate.scalar_one_or_none() is not None:
            raise ProjectServiceError(
                "remote_project_already_exists",
                f"a project already uses {remote_path} on daemon {remote_daemon}",
                status_code=409,
            )

    now = _utcnow()
    project = ConsoleProjectModel(
        id=f"project-{uuid.uuid4().hex[:12]}",
        name=_clean_name(raw_name, path.name),
        path=str(path),
        remote_daemon_id=remote_daemon,
        remote_path=remote_path,
        created_at=now,
        updated_at=now,
    )
    db.add(project)
    await db.commit()
    return _project_dict(project)


async def update_project(
    db: AsyncSession,
    project_id: str,
    *,
    raw_name: str,
) -> dict[str, Any] | None:
    project = await db.get(ConsoleProjectModel, project_id)
    if project is None:
        return None
    project.name = _clean_name(raw_name, project.name)
    project.updated_at = _utcnow()
    await db.commit()
    return await get_project(db, project_id)


async def delete_project(
    db: AsyncSession,
    project_id: str,
) -> bool:
    project = await db.get(ConsoleProjectModel, project_id)
    if project is None:
        return False
    await db.delete(project)
    await db.commit()
    return True


async def open_project(
    db: AsyncSession,
    project_id: str,
) -> dict[str, Any] | None:
    project = await db.get(ConsoleProjectModel, project_id)
    if project is None:
        return None
    if not _is_available(Path(project.path)):
        raise ProjectServiceError(
            "project_unavailable",
            "project directory is missing or no longer readable",
            status_code=409,
        )
    project.last_opened_at = _utcnow()
    project.updated_at = project.updated_at or project.last_opened_at
    await db.commit()
    return await get_project(db, project_id)


async def list_directories(
    config: ServerConfig,
    raw_path: str | None,
) -> dict[str, Any]:
    roots = _allowed_roots(config)
    if not roots:
        raise ProjectServiceError(
            "no_workspace_roots", "no console workspace roots are configured"
        )
    if raw_path:
        current = resolve_project_path(config, raw_path)
    else:
        cwd = Path.cwd().resolve()
        current = next(
            (root for root in roots if _is_relative_to(cwd, root)),
            roots[0],
        )
    if not _is_available(current):
        raise ProjectServiceError(
            "path_not_directory", "directory path must exist and be readable"
        )

    parent: Path | None = None
    if any(_is_relative_to(current.parent, root) for root in roots):
        parent = current.parent

    directories: list[dict[str, str]] = []
    try:
        candidates = list(current.iterdir())
    except OSError as exc:
        raise ProjectServiceError(
            "directory_not_readable", f"cannot read directory: {exc}"
        ) from None
    for child in candidates:
        if len(directories) >= _MAX_DIRECTORY_ENTRIES:
            break
        try:
            resolved = child.resolve(strict=False)
            if not resolved.is_dir():
                continue
            if not any(_is_relative_to(resolved, root) for root in roots):
                continue
        except OSError:
            continue
        directories.append({"name": child.name, "path": str(resolved)})
    directories.sort(key=lambda item: item["name"].lower())

    return {
        "roots": [str(root) for root in roots],
        "parent": str(parent) if parent is not None else None,
        "current": str(current),
        "directories": directories,
    }


__all__ = [
    "ProjectServiceError",
    "create_project",
    "delete_project",
    "get_project",
    "list_directories",
    "list_projects",
    "open_project",
    "resolve_project_path",
    "update_project",
]
