"""Browser-console project management routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from ...comm.errors import RPCRejected, WSRPCUnavailable, WSRPCUncertain
from ..database import get_db
from ..models import JobModel
from ..schemas import (
    ConsoleDirectoryPickResponse,
    ConsoleDirectoryListResponse,
    ConsoleProjectCreate,
    ConsoleProjectListResponse,
    ConsoleProjectOut,
    ConsoleProjectUpdate,
    ConsoleSopRenameRequest,
)
from ..services.project_service import (
    ProjectServiceError,
    create_project,
    delete_project,
    get_project,
    list_directories,
    list_projects,
    open_project,
    resolve_project_path,
    update_project,
)
from ..services.native_picker import (
    NativePickerError,
    native_picker_allowed_for_host,
    pick_native_directory,
)

router = APIRouter(prefix="/api/console", tags=["console projects"])
_SOP_NAME_RE = re.compile(r"^[^\0]+\.ya?ml$", re.IGNORECASE)


def _error(exc: ProjectServiceError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"type": exc.code, "message": str(exc)},
    )


def _remote_link(request: Request, daemon_id: str):
    """Return the live ``DaemonLink`` for a configured remote target.

    Local daemons have no reverse-dial link, so a ``daemon_id`` that is not
    in ``app.state.daemon_links`` is either local (the console omits the
    parameter for those) or unknown — both are rejected here.
    """
    links = getattr(request.app.state, "daemon_links", {}) or {}
    link = links.get(daemon_id)
    if link is None:
        raise HTTPException(
            status_code=404,
            detail={
                "type": "unknown_remote_daemon",
                "message": f"'{daemon_id}' is not a configured remote daemon",
            },
        )
    return link


def _rpc_error(exc: Exception) -> HTTPException:
    """Map a reverse-dial failure onto a console-facing HTTP error."""
    if isinstance(exc, WSRPCUnavailable):
        return HTTPException(
            status_code=503,
            detail={"type": "daemon_offline", "message": "daemon is offline"},
        )
    if isinstance(exc, WSRPCUncertain):
        return HTTPException(
            status_code=504,
            detail={"type": "daemon_timeout", "message": "daemon did not answer in time"},
        )
    body = exc.body if isinstance(exc, RPCRejected) and isinstance(exc.body, dict) else {}
    error_type = str(body.get("type") or "daemon_rejected")
    message = str(body.get("message") or "daemon rejected the request")
    # ``path_error`` / ``validation_error`` are user-input problems → 400.
    # ``unknown_method`` means an older daemon without ``fs.list`` → 502.
    status_code = 400 if error_type in {"path_error", "validation_error"} else 502
    return HTTPException(
        status_code=status_code,
        detail={"type": error_type, "message": message},
    )


async def _list_remote_directories(
    request: Request, daemon_id: str, path: str | None
) -> dict:
    link = _remote_link(request, daemon_id)
    try:
        return await link.list_directory(path)
    except (WSRPCUnavailable, WSRPCUncertain, RPCRejected) as exc:
        raise _rpc_error(exc) from None


@router.get("/directories", response_model=ConsoleDirectoryListResponse)
async def directories(
    request: Request,
    path: str | None = Query(None),
    daemon_id: str | None = Query(
        None,
        description=(
            "Remote daemon whose filesystem to browse. Omit (or leave empty) "
            "to browse the Server machine."
        ),
    ),
) -> ConsoleDirectoryListResponse:
    if daemon_id:
        result = await _list_remote_directories(request, daemon_id, path)
        return ConsoleDirectoryListResponse(**result)
    try:
        result = await list_directories(request.app.state.config, path)
    except ProjectServiceError as exc:
        raise _error(exc) from None
    return ConsoleDirectoryListResponse(**result)


@router.post(
    "/directories/pick",
    response_model=ConsoleDirectoryPickResponse,
)
async def pick_directory(request: Request) -> ConsoleDirectoryPickResponse:
    config = request.app.state.config
    if not native_picker_allowed_for_host(config.host):
        raise HTTPException(
            status_code=403,
            detail={
                "type": "native_picker_local_only",
                "message": "native directory picker requires a loopback server",
            },
        )
    try:
        picked = await pick_native_directory()
        if picked is None:
            return ConsoleDirectoryPickResponse(path=None)
        path = resolve_project_path(config, picked)
        if not path.is_dir():
            raise ProjectServiceError(
                "path_not_directory",
                "selected path must be an existing directory",
            )
    except NativePickerError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"type": exc.code, "message": str(exc)},
        ) from None
    except ProjectServiceError as exc:
        raise _error(exc) from None
    return ConsoleDirectoryPickResponse(path=str(path))


@router.post("/sop/file/rename")
async def rename_sop_file(
    payload: ConsoleSopRenameRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    try:
        source = resolve_project_path(request.app.state.config, payload.path)
    except ProjectServiceError as exc:
        raise _error(exc) from None
    if not _SOP_NAME_RE.match(source.name):
        raise HTTPException(status_code=400, detail="only YAML files are supported")
    if not source.is_file():
        raise HTTPException(status_code=404, detail="SOP file not found")

    new_name = payload.new_name.strip()
    if (
        not new_name
        or new_name.startswith(".")
        or Path(new_name).name != new_name
        or "/" in new_name
        or "\\" in new_name
    ):
        raise HTTPException(status_code=400, detail="invalid SOP filename")
    if not _SOP_NAME_RE.match(new_name):
        raise HTTPException(status_code=400, detail="only YAML files are supported")

    destination = source.with_name(new_name)
    if destination == source:
        return {"path": str(source), "label": source.name}
    if destination.exists():
        raise HTTPException(status_code=409, detail="SOP filename already exists")
    await db.execute(
        update(JobModel)
        .where(
            JobModel.root_job_id == JobModel.job_id,
            JobModel.job_type == "sop",
            JobModel.sop_template_path == str(source),
        )
        .values(sop_template_path=str(destination))
    )
    try:
        source.rename(destination)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"cannot rename SOP file: {exc}",
        ) from None
    await db.commit()
    return {"path": str(destination), "label": destination.name}


@router.get("/projects", response_model=ConsoleProjectListResponse)
async def projects(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ConsoleProjectListResponse:
    rows = await list_projects(db, request.app.state.config)
    return ConsoleProjectListResponse(
        projects=[ConsoleProjectOut(**row) for row in rows]
    )


@router.post(
    "/projects",
    response_model=ConsoleProjectOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_project_route(
    payload: ConsoleProjectCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> ConsoleProjectOut:
    remote_daemon = (payload.remote_daemon_id or "").strip() or None
    remote_path = (payload.remote_path or "").strip() or None
    if (remote_daemon is None) != (remote_path is None):
        raise HTTPException(
            status_code=400,
            detail={
                "type": "remote_pair_incomplete",
                "message": "remote_daemon_id and remote_path must be provided together",
            },
        )
    if remote_daemon is not None and remote_path is not None:
        # Validate against the daemon's own filesystem/allowlist. An offline
        # daemon is tolerated (the project is stored and validated on first
        # use) so a project can be created before the machine comes up.
        link = _remote_link(request, remote_daemon)
        try:
            await link.list_directory(remote_path)
        except WSRPCUnavailable:
            pass
        except (WSRPCUncertain, RPCRejected) as exc:
            raise _rpc_error(exc) from None
    try:
        row = await create_project(
            db,
            request.app.state.config,
            raw_path=payload.path,
            raw_name=payload.name,
            remote_daemon_id=remote_daemon,
            raw_remote_path=remote_path,
        )
    except ProjectServiceError as exc:
        raise _error(exc) from None
    return ConsoleProjectOut(**row)


@router.get("/projects/{project_id}", response_model=ConsoleProjectOut)
async def project(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> ConsoleProjectOut:
    row = await get_project(db, project_id)
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return ConsoleProjectOut(**row)


@router.patch("/projects/{project_id}", response_model=ConsoleProjectOut)
async def update_project_route(
    project_id: str,
    payload: ConsoleProjectUpdate,
    db: AsyncSession = Depends(get_db),
) -> ConsoleProjectOut:
    try:
        row = await update_project(db, project_id, raw_name=payload.name)
    except ProjectServiceError as exc:
        raise _error(exc) from None
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return ConsoleProjectOut(**row)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_route(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await delete_project(db, project_id):
        raise HTTPException(status_code=404, detail="project not found")


@router.post("/projects/{project_id}/open", response_model=ConsoleProjectOut)
async def open_project_route(
    project_id: str,
    db: AsyncSession = Depends(get_db),
) -> ConsoleProjectOut:
    try:
        row = await open_project(db, project_id)
    except ProjectServiceError as exc:
        raise _error(exc) from None
    if row is None:
        raise HTTPException(status_code=404, detail="project not found")
    return ConsoleProjectOut(**row)


__all__ = ["router"]
