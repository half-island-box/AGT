"""Binding service layer — identity ↔ runtime mappings (Phase 4 Step 2).

A binding says "this identity may run on that daemon's provider". Bindings
are the input to the selection algorithm (selection_service.select_runtime);
they carry no runtime behavior of their own.

Key invariant (design D2): at most one binding per identity has
``is_default=true``. ``set_default_binding`` and ``create_binding(...,
is_default=True)`` enforce it inside a single session transaction.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.models import IdentityBindingModel, IdentityModel

log = logging.getLogger(__name__)


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp — matches the repo-wide convention
    (DB columns store naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _binding_to_dict(b: IdentityBindingModel) -> dict[str, Any]:
    return {
        "id": b.id,
        "identity_did": b.identity_did,
        "daemon_id": b.daemon_id,
        "provider": b.provider,
        "priority": b.priority,
        "is_default": bool(b.is_default),
        "created_at": b.created_at,
    }


async def _unflag_defaults(did: str, db: AsyncSession) -> None:
    """Clear every is_default flag on this identity's bindings."""
    await db.execute(
        update(IdentityBindingModel)
        .where(
            IdentityBindingModel.identity_did == did,
            IdentityBindingModel.is_default == True,  # noqa: E712
        )
        .values(is_default=False)
    )


async def create_binding(
    did: str,
    payload: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any]:
    """Create a binding. When ``is_default=True``, atomically unflag the
    identity's other defaults in the same transaction.

    Raises:
        ValueError: identity not found, or the (did, daemon, provider)
            triple already exists.
    """
    identity = await db.get(IdentityModel, did)
    if identity is None:
        raise ValueError(f"identity not found: {did}")

    daemon_id = payload.get("daemon_id")
    provider = payload.get("provider")
    is_default = bool(payload.get("is_default", False))

    existing = await db.execute(
        select(IdentityBindingModel).where(
            IdentityBindingModel.identity_did == did,
            IdentityBindingModel.daemon_id == daemon_id,
            IdentityBindingModel.provider == provider,
        )
    )
    if existing.scalar_one_or_none():
        raise ValueError(
            f"binding already exists for identity '{did}' on "
            f"daemon '{daemon_id}' provider '{provider}'"
        )

    if is_default:
        await _unflag_defaults(did, db)

    binding = IdentityBindingModel(
        identity_did=did,
        daemon_id=daemon_id,
        provider=provider,
        priority=int(payload.get("priority", 0)),
        is_default=is_default,
        created_at=_utcnow_naive().isoformat(),
    )
    db.add(binding)
    await db.commit()
    await db.refresh(binding)

    log.info("Created binding %d: %s on %s/%s", binding.id, did, daemon_id, provider)
    return _binding_to_dict(binding)


async def list_bindings(
    did: str,
    daemon_id: str | None = None,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """List bindings for an identity, optionally filtered by daemon."""
    if db is None:
        raise ValueError("Database session required")

    stmt = (
        select(IdentityBindingModel)
        .where(IdentityBindingModel.identity_did == did)
        .order_by(IdentityBindingModel.id.asc())
    )
    if daemon_id:
        stmt = stmt.where(IdentityBindingModel.daemon_id == daemon_id)

    result = await db.execute(stmt)
    return [_binding_to_dict(b) for b in result.scalars().all()]


async def list_bindings_by_daemon(
    daemon_id: str,
    db: AsyncSession,
) -> list[dict[str, Any]]:
    """List every binding on a daemon, across all identities.

    The inverse view of :func:`list_bindings`: instead of "which runtimes
    is identity X bound to", answer "which identities are bound to daemon
    Y". Ordered by identity_did then provider for a stable read.
    """
    stmt = (
        select(IdentityBindingModel)
        .where(IdentityBindingModel.daemon_id == daemon_id)
        .order_by(
            IdentityBindingModel.identity_did.asc(),
            IdentityBindingModel.provider.asc(),
        )
    )
    result = await db.execute(stmt)
    return [_binding_to_dict(b) for b in result.scalars().all()]


async def delete_binding(
    did: str,
    binding_id: int,
    db: AsyncSession,
) -> bool:
    """Delete one binding. Returns False when it doesn't exist.

    Deleting the default binding leaves the identity without any default
    (no auto-promotion; selection falls back to priority).
    """
    binding = await db.execute(
        select(IdentityBindingModel).where(
            IdentityBindingModel.id == binding_id,
            IdentityBindingModel.identity_did == did,
        )
    )
    row = binding.scalar_one_or_none()
    if row is None:
        return False

    await db.delete(row)
    await db.commit()
    log.info("Deleted binding %d (identity %s)", binding_id, did)
    return True


async def set_default_binding(
    did: str,
    binding_id: int,
    db: AsyncSession,
) -> dict[str, Any]:
    """Atomically switch the identity's default to ``binding_id``.

    Both UPDATEs run in one session transaction: unflag the old default,
    set the new one — no intermediate state is observable.

    Raises:
        ValueError: binding not found for this identity.
    """
    found = await db.execute(
        select(IdentityBindingModel).where(
            IdentityBindingModel.id == binding_id,
            IdentityBindingModel.identity_did == did,
        )
    )
    if found.scalar_one_or_none() is None:
        raise ValueError(f"binding {binding_id} not found for identity {did}")

    await _unflag_defaults(did, db)
    await db.execute(
        update(IdentityBindingModel)
        .where(IdentityBindingModel.id == binding_id)
        .values(is_default=True)
    )
    await db.commit()

    refreshed = await db.execute(
        select(IdentityBindingModel).where(IdentityBindingModel.id == binding_id)
    )
    binding = refreshed.scalar_one()
    log.info("Set default binding %d for identity %s", binding_id, did)
    return _binding_to_dict(binding)


__all__ = [
    "create_binding",
    "list_bindings",
    "list_bindings_by_daemon",
    "delete_binding",
    "set_default_binding",
]
