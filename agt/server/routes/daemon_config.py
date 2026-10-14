"""Daemon execution configuration API.

Thin FastAPI wrapper around the persistent config logic in
``agt.daemon.config``. These endpoints read and write ``~/.agt/daemon.json``
on the machine where the Server process is running; they are not a remote
management interface for other daemons.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ...daemon.config import (
    DaemonConfigError,
    ResolvedSetting,
    daemon_config_file_path,
    resolve_daemon_settings,
    update_daemon_config_file,
)

router = APIRouter(prefix="/api/daemon/config", tags=["daemon config"])


class _DaemonConfigSettingOut(BaseModel):
    """One managed setting after precedence resolution."""

    value: int | str | list[str]
    source: str
    saved: int | str | list[str] | None = None
    default: int | str | list[str] | None = None
    env_name: str | None = None


class DaemonConfigShowResponse(BaseModel):
    """Response shape for ``GET /api/daemon/config``."""

    config_file: str
    settings: dict[str, _DaemonConfigSettingOut]


class DaemonConfigSetRequest(BaseModel):
    """Body for ``POST /api/daemon/config``.

    Only supplied keys are updated; omitted keys are left unchanged.
    ``None`` values are ignored rather than treated as removals.
    """

    max_concurrent: int | None = None
    agent_timeout: int | None = None
    work_dir: str | None = None
    agent_cwd_allowlist: list[str] | None = None


class DaemonConfigSetResponse(DaemonConfigShowResponse):
    """Response shape for ``POST /api/daemon/config``."""


# Map JSON key → human-readable label used only if we add per-key metadata later.
_MANAGED_KEYS: tuple[str, ...] = (
    "max_concurrent",
    "agent_timeout",
    "work_dir",
    "agent_cwd_allowlist",
)


def _serialize_value(value: Any) -> int | str | list[str]:
    """Convert Path / list[Path] values to JSON-friendly primitives."""
    from pathlib import Path

    if isinstance(value, Path):
        return str(value)
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, int):
        return value
    return str(value)


def _serialize_setting(setting: ResolvedSetting) -> _DaemonConfigSettingOut:
    """Project a ResolvedSetting onto the response model."""
    return _DaemonConfigSettingOut(
        value=_serialize_value(setting.value),
        source=setting.source,
        saved=_serialize_value(setting.file_value) if setting.file_value is not None else None,
        default=_serialize_value(setting.default_value) if setting.default_value is not None else None,
        env_name=setting.env_name,
    )


def _build_show_response() -> DaemonConfigShowResponse:
    """Resolve current settings and return the show shape."""
    settings = resolve_daemon_settings()
    return DaemonConfigShowResponse(
        config_file=str(daemon_config_file_path()),
        settings={
            key: _serialize_setting(settings[key])
            for key in _MANAGED_KEYS
            if key in settings
        },
    )


@router.get("", response_model=DaemonConfigShowResponse)
async def show_daemon_config() -> DaemonConfigShowResponse:
    """Return the effective next-start daemon configuration and its sources.

    Mirrors ``agt daemon config show``. Resolution order is:
    environment variable > ``~/.agt/daemon.json`` > built-in default.
    """
    try:
        return _build_show_response()
    except DaemonConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


@router.post("", response_model=DaemonConfigSetResponse)
async def set_daemon_config(req: DaemonConfigSetRequest) -> DaemonConfigSetResponse:
    """Persist one or more daemon execution-resource settings.

    Mirrors ``agt daemon config set``. The file is written atomically with
    owner-only permissions after full validation. Changes take effect on the
    next daemon restart.
    """
    updates: dict[str, Any] = {}
    if req.max_concurrent is not None:
        updates["max_concurrent"] = req.max_concurrent
    if req.agent_timeout is not None:
        updates["agent_timeout"] = req.agent_timeout
    if req.work_dir is not None:
        updates["work_dir"] = req.work_dir
    if req.agent_cwd_allowlist is not None:
        updates["agent_cwd_allowlist"] = req.agent_cwd_allowlist

    if not updates:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="at least one setting is required",
        )

    try:
        update_daemon_config_file(updates)
    except DaemonConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    try:
        return _build_show_response()
    except DaemonConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc


__all__ = ["router"]
