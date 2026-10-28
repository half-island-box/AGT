"""Browser-console project management routes."""

from __future__ import annotations

import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
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


@router.get("/directories", response_model=ConsoleDirectoryListResponse)
async def directories(
    request: Request,
    path: str | None = Query(None),
) -> ConsoleDirectoryListResponse:
    try:
        result = await list_directories(request.app.state.config, path)
    except ProjectServiceError as exc:
        raise _error(exc) from None
    return ConsoleDirectoryListResponse(**result)


@router.post("/sop/file/rename")
async def rename_sop_file(
    payload: ConsoleSopRenameRequest,
    request: Request,
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
    try:
        source.rename(destination)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"cannot rename SOP file: {exc}",
        ) from None
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
    try:
        row = await create_project(
            db,
            request.app.state.config,
            raw_path=payload.path,
            raw_name=payload.name,
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
