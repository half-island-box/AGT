"""Browser-console project management routes."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    ConsoleDirectoryListResponse,
    ConsoleProjectCreate,
    ConsoleProjectListResponse,
    ConsoleProjectOut,
    ConsoleProjectUpdate,
)
from ..services.project_service import (
    ProjectServiceError,
    create_project,
    delete_project,
    get_project,
    list_directories,
    list_projects,
    open_project,
    update_project,
)

router = APIRouter(prefix="/api/console", tags=["console projects"])


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
