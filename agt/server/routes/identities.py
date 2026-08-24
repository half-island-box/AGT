"""Identity CRUD routes (Phase 4 Step 1).

Endpoints for creating, reading, updating, deprecating, revoking, and
deleting identities. AGENTS.md files are written to
``~/.agt/identities/<did>/AGENTS.md`` on creation and are NOT automatically
overwritten on updates (use sync-files for forced regeneration).
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..schemas import (
    IdentityBindingCreate,
    IdentityBindingOut,
    IdentityCreate,
    IdentityOut,
    IdentityUpdate,
    ListBindingsResponse,
    ListIdentitiesResponse,
    ResolveRequest,
    ResolveResponse,
)
from ..services.binding_service import (
    create_binding,
    delete_binding,
    list_bindings,
    set_default_binding,
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
from ..services.selection_service import SelectionError, select_runtime

router = APIRouter(prefix="/api/identities", tags=["identities"])


def _root_dir() -> Path:
    """Home directory root for AGENTS.md files (~/.agt/identities/...)."""
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
    """Create a new identity with DID generation and AGENTS.md write."""
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


@router.post("/resolve", response_model=ResolveResponse)
async def resolve_identity_route(
    payload: ResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> ResolveResponse:
    """Dry-run the selection algorithm: which runtime would this identity
    run on? Side-effect free (no writes, no task creation).

    ``identity_did`` missing → 400: the caller decides whether to take the
    no-identity legacy path; there is no implicit default identity.

    Registered BEFORE the ``/{did}`` routes so the static "resolve"
    segment is not swallowed as a DID.
    """
    if not payload.identity_did:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="identity_did is required (no implicit default identity)",
        )
    try:
        result = await select_runtime(
            payload.identity_did,
            payload.runtime_hint,
            payload.daemon_id,
            db,
        )
    except SelectionError as exc:
        raise _selection_error_to_http(exc)
    return ResolveResponse(
        daemon_id=result.daemon_id,
        provider=result.provider,
        identity_did=result.identity_did,
    )


def _selection_error_to_http(exc: SelectionError) -> HTTPException:
    """Map a SelectionError to an HTTPException with structured detail.

    Shared by the resolve endpoint and the submit-task endpoint so the
    HTTP shape is consistent across the two callers.
    """
    code = (
        status.HTTP_404_NOT_FOUND
        if exc.err_type == "identity_not_found"
        else status.HTTP_400_BAD_REQUEST
    )
    return HTTPException(
        status_code=code,
        detail={"type": exc.err_type, "message": str(exc), **exc.context},
    )


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
    """Update an identity (partial update; AGENTS.md not overwritten)."""
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
    """Delete an identity (and its ~/.agt/identities/<slug>/ dir), refusing
    if any task references it."""
    try:
        deleted = await delete_identity(did, db, _root_dir())
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
    """Force regenerate the AGENTS.md file from current database state."""
    identity = await sync_agent_md(did, db, _root_dir())
    if identity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"identity not found: {did}",
        )
    return _to_out(identity)


# ── Bindings (Phase 4 Step 2) ──────────────────────────────────────


@router.get("/{did}/bindings", response_model=ListBindingsResponse)
async def list_bindings_route(
    did: str,
    daemon_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> ListBindingsResponse:
    """List an identity's bindings, optionally filtered by daemon."""
    bindings = await list_bindings(did, daemon_id=daemon_id, db=db)
    return ListBindingsResponse(
        bindings=[IdentityBindingOut(**b) for b in bindings]
    )


@router.post(
    "/{did}/bindings",
    response_model=IdentityBindingOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_binding_route(
    did: str,
    payload: IdentityBindingCreate,
    db: AsyncSession = Depends(get_db),
) -> IdentityBindingOut:
    """Create a binding; ``is_default=true`` atomically unflags siblings."""
    try:
        binding = await create_binding(did, payload.model_dump(), db)
    except ValueError as exc:
        message = str(exc)
        if "already exists" in message:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=message
            )
        if "not found" in message:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail=message
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=message
        )
    return IdentityBindingOut(**binding)


@router.delete("/{did}/bindings/{binding_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_binding_route(
    did: str,
    binding_id: int,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete one binding by id (scoped to the given identity)."""
    deleted = await delete_binding(did, binding_id, db)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"binding {binding_id} not found for identity {did}",
        )


@router.post("/{did}/bindings/{binding_id}/default", response_model=IdentityBindingOut)
async def set_default_binding_route(
    did: str,
    binding_id: int,
    db: AsyncSession = Depends(get_db),
) -> IdentityBindingOut:
    """Atomically switch the identity's default binding."""
    try:
        binding = await set_default_binding(did, binding_id, db)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)
        )
    return IdentityBindingOut(**binding)
