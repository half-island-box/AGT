"""Identity CRUD routes (Phase 4 Step 1).

Endpoints for creating, reading, updating, deprecating, revoking, and
deleting identities. AGENT.md files are written to
``~/.agt/identities/<did>/AGENT.md`` on creation and are NOT automatically
overwritten on updates (use sync-files for forced regeneration).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    IdentityCreate,
    IdentityOut,
    IdentityUpdate,
    ListIdentitiesResponse,
)
from ..services.identity_service import (
    create_identity,
    delete_identity,
    deprecate_identity,
    get_identity,
    list_identities,
    revoke_identity,
    sync_agent_md,
    update_identity,
)

router = APIRouter(prefix="/api/identities", tags=["identities"])


def _root_dir() -> Path:
    """Home directory root for AGENT.md files (~/.agt/identities/...)."""
    return Path.home()


def _to_out(identity: dict) -> IdentityOut:
    """Convert an identity dict to IdentityOut response model."""
    return IdentityOut(**identity)


@router.get("", response_model=ListIdentitiesResponse)
async def list_identities_route(
    daemon_id: str | None = None,
    status_filter: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> ListIdentitiesResponse:
    """List identities with optional filtering by daemon_id and status."""
    identities = await list_identities(
        daemon_id=daemon_id,
        status=status_filter,
        db=db,
    )
    return ListIdentitiesResponse(
        identities=[_to_out(identity) for identity in identities]
    )


@router.post("", response_model=IdentityOut, status_code=status.HTTP_201_CREATED)
async def create_identity_route(
    payload: IdentityCreate,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Create a new identity with DID generation and AGENT.md write."""
    try:
        did = await create_identity(
            payload.model_dump(),
            db,
            _root_dir(),
        )
    except ValueError as exc:
        # ValueError from service = validation or duplicate error
        if "already exists" in str(exc):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        )

    identity = await get_identity(did, db)
    return _to_out(identity)


@router.get("/{did}", response_model=IdentityOut)
async def get_identity_route(
    did: str,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Retrieve a single identity by DID."""
    identity = await get_identity(did, db)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)


@router.put("/{did}", response_model=IdentityOut)
async def update_identity_route(
    did: str,
    payload: IdentityUpdate,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Update an identity (partial update; AGENT.md not overwritten)."""
    identity = await update_identity(did, payload.model_dump(), db)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)


@router.delete("/{did}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_identity_route(
    did: str,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete an identity, refusing if any task references it."""
    try:
        deleted = await delete_identity(did, db)
    except ValueError as exc:
        # Task reference exists — conflict
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )


@router.post("/{did}/deprecate", response_model=IdentityOut)
async def deprecate_identity_route(
    did: str,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Soft-delete an identity (status='deprecated')."""
    identity = await deprecate_identity(did, db)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)


@router.post("/{did}/revoke", response_model=IdentityOut)
async def revoke_identity_route(
    did: str,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Hard-revoke an identity (status='revoked')."""
    identity = await revoke_identity(did, db)
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)


@router.post("/{did}/sync-files", response_model=IdentityOut)
async def sync_identity_files_route(
    did: str,
    db: AsyncSession = Depends(get_db),
) -> IdentityOut:
    """Force regenerate the AGENT.md file from current database state."""
    identity = await sync_agent_md(did, db, _root_dir())
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)
