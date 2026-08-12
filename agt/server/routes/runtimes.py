"""Runtime registration routes."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..models import RuntimeModel
from ..schemas import (
    ListRuntimesResponse,
    RegisterRequest,
    RegisterResponse,
    RuntimeOut,
)

router = APIRouter(prefix="/api/runtimes", tags=["runtimes"])


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register_runtimes(
    payload: RegisterRequest,
    db: AsyncSession = Depends(get_db),
) -> RegisterResponse:
    """Replace this daemon's runtime entries. Upserts by (daemon_id, provider)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    # Wipe out the daemon's old entries first — fresh start on each registration.
    await db.execute(delete(RuntimeModel).where(RuntimeModel.daemon_id == payload.daemon_id))

    count = 0
    for runtime in payload.runtimes:
        db.add(
            RuntimeModel(
                daemon_id=payload.daemon_id,
                provider=runtime.type,
                name=runtime.name,
                version=runtime.version,
                status=runtime.status,
                device_name=runtime.device_name,
                last_seen=now,
            )
        )
        count += 1
    await db.commit()
    return RegisterResponse(registered=count)


@router.get("", response_model=ListRuntimesResponse)
async def list_runtimes(
    db: AsyncSession = Depends(get_db),
) -> ListRuntimesResponse:
    """Return all runtimes registered by all daemons."""
    rows = (
        await db.execute(
            select(RuntimeModel).order_by(
                RuntimeModel.daemon_id, RuntimeModel.provider
            )
        )
    ).scalars().all()
    return ListRuntimesResponse(
        runtimes=[
            RuntimeOut(
                name=row.name,
                type=row.provider,
                version=row.version,
                status=row.status,
                device_name=row.device_name,
                daemon_id=row.daemon_id,
            )
            for row in rows
        ]
    )


@router.delete("/unregister/{daemon_id}", status_code=status.HTTP_200_OK)
async def unregister_runtimes(
    daemon_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Remove all runtime registrations for a daemon (called on shutdown)."""
    result = await db.execute(
        delete(RuntimeModel).where(RuntimeModel.daemon_id == daemon_id)
    )
    await db.commit()
    return {"removed": result.rowcount}


__all__ = ["router"]