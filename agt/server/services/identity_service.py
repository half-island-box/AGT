"""Identity service layer for CRUD operations and AGENTS.md file management.

Phase 4 Step 1: Identities can be created, read, updated, deprecated, revoked,
and deleted. Each identity has a DID (did:agt:<daemon_id>:<slug>) and optionally
an AGENTS.md file written to ~/.agt/identities/<did>/AGENTS.md.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.models import (
    IdentityBindingModel,
    IdentityModel,
    RuntimeModel,
)

from .runtime_service import is_stale

log = logging.getLogger(__name__)

# Slug validation: lowercase alphanumeric with hyphens, 1-64 chars, no leading/trailing hyphens
_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$")


def _utcnow_naive() -> datetime:
    """Naive UTC timestamp — matches the repo-wide convention
    (see runtime_service._utcnow_naive; DB columns store naive UTC)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _dir_name(did: str) -> str:
    """Extract directory name from DID, with hash fallback for collisions.

    The DID format is ``did:agt:<slug>`` (global slug — namespace isolation
    was removed in 2026-08). We use the slug as the directory name. Hash
    fallback is kept as a defensive guard against malformed inputs.

    Args:
        did: The full DID string

    Returns:
        Directory name for storing AGENTS.md files
    """
    parts = did.split(":")
    if len(parts) != 3 or parts[0] != "did":
        # Fallback for malformed DID: use hash of entire DID
        return f"identity-{hashlib.sha256(did.encode()).hexdigest()[:8]}"

    slug = parts[-1]  # Last part after the final colon

    # Check if slug looks valid (alphanumeric with hyphens)
    if _SLUG_PATTERN.match(slug):
        return slug

    # Fallback: slug-<hash>
    hash_suffix = hashlib.sha256(did.encode()).hexdigest()[:8]
    return f"{slug}-{hash_suffix}"


def normalize_identity_did(value: str) -> str:
    """Normalize a user-supplied ``--identity`` value to a full DID.

    The canonical DID is ``did:agt:<slug>`` (namespace isolation was removed
    in 2026-08). Users often pass a bare slug, or mis-type the scheme (e.g.
    ``id:agt:code-reviewer``). Both are caught here so they fail with a clear
    message at the CLI instead of a cryptic ``identity_not_found`` 404 from
    the server.

    Accepts:
      - an already-full DID ``did:agt:<slug>`` (returned as-is, stripped);
      - a bare slug ``code-reviewer`` (prefixed to ``did:agt:code-reviewer``).

    Raises:
        ValueError: empty input, a non-``did:agt:`` scheme, or an invalid slug.
    """
    value = (value or "").strip()
    if not value:
        raise ValueError("identity must not be empty")

    prefix = "did:agt:"
    if value.startswith(prefix):
        slug = value[len(prefix):]
        if not _SLUG_PATTERN.match(slug):
            raise ValueError(
                f"invalid identity '{value}': the part after 'did:agt:' must "
                "be a lowercase alphanumeric slug with hyphens"
            )
        return value

    # A colon that isn't the did:agt: scheme is almost always a typo
    # (e.g. 'id:agt:...'); reject it rather than let it 404 on the server.
    if ":" in value:
        scheme = value.split(":", 1)[0]
        raise ValueError(
            f"invalid identity '{value}': unknown scheme '{scheme}:' — "
            "expected 'did:agt:<slug>' or a bare slug"
        )

    # No scheme → treat as a bare slug and expand to the full DID.
    if not _SLUG_PATTERN.match(value):
        raise ValueError(
            f"invalid identity slug '{value}': must be 1-64 chars, lowercase "
            "alphanumeric with hyphens, no leading/trailing hyphens"
        )
    return f"{prefix}{value}"


def _render_agent_md(identity: IdentityModel | dict[str, Any]) -> str:
    """Render identity metadata into AGENTS.md markdown format.

    Args:
        identity: Identity model or dict containing did, name, description, capabilities, status, daemon_id

    Returns:
        Complete markdown content for AGENTS.md file
    """
    if isinstance(identity, dict):
        did = identity.get("did", "")
        name = identity.get("name", "")
        description = identity.get("description", "")
        capabilities = identity.get("capabilities", "[]")
        status = identity.get("status", "active")
        daemon_id = identity.get("daemon_id", "")
    else:
        did = identity.did
        name = identity.name
        description = identity.description
        capabilities = identity.capabilities
        status = identity.status
        daemon_id = identity.daemon_id

    # Parse capabilities JSON if it's a string
    if isinstance(capabilities, str):
        try:
            caps_list = json.loads(capabilities)
        except json.JSONDecodeError:
            caps_list = []
    else:
        caps_list = capabilities or []

    caps_md = "\n".join(f"- {cap}" for cap in caps_list) if caps_list else "*(No capabilities defined)*"

    return f"""<!-- Auto-generated by AGT. Manual edits will be preserved unless regenerated. -->

# {name}

**DID**: `{did}`
**Daemon**: `{daemon_id}`
**Status**: `{status}`

## Description
{description or "*(No description provided)*"}

## Capabilities
{caps_md}

## Working Style
*(Optional: Describe the working style, preferences, or constraints for this identity.)*
"""


def write_agent_md(
    identity: IdentityModel | dict[str, Any],
    root: Path,
    force: bool = False,
) -> Path:
    """Write AGENTS.md file for an identity, idempotent by default.

    Args:
        identity: Identity model or dict containing did and other metadata
        root: Root directory for identities (typically ~/.agt)
        force: If True, overwrite existing file; if False, skip if exists

    Returns:
        Path to the AGENTS.md file (whether created or skipped)
    """
    if isinstance(identity, dict):
        did = identity.get("did", "")
    else:
        did = identity.did

    dir_name = _dir_name(did)
    target = root / ".agt" / "identities" / dir_name / "AGENTS.md"

    # Create directory if it doesn't exist
    target.parent.mkdir(parents=True, exist_ok=True)

    # Skip if file exists and not forcing
    if target.exists() and not force:
        log.debug(f"AGENTS.md already exists at {target}, skipping (use force=True to overwrite)")
        return target

    # Render and write
    content = _render_agent_md(identity)
    target.write_text(content, encoding="utf-8")
    log.info(f"Wrote AGENTS.md to {target}")
    return target


async def create_identity(
    payload: dict[str, Any],
    db: AsyncSession,
    root_dir: Path,
) -> str:
    """Create a new identity with DID generation and AGENTS.md file write.

    Args:
        payload: IdentityCreate payload with daemon_id, name, slug, description, capabilities, etc.
        db: Database session
        root_dir: Root directory for AGENTS.md files (typically ~/.agt)

    Returns:
        The generated DID

    Raises:
        ValueError: If slug format is invalid or (daemon_id, slug) already exists
    """
    daemon_id = payload.get("daemon_id")
    name = payload.get("name")

    # Derive slug from name if not provided
    slug = payload.get("slug")
    if not slug:
        # Lowercase, replace spaces with hyphens, remove invalid chars
        slug = name.lower().replace(" ", "-")
        slug = re.sub(r"[^a-z0-9-]", "", slug)
        # Ensure no consecutive hyphens
        slug = re.sub(r"-+", "-", slug)
        # Remove leading/trailing hyphens
        slug = slug.strip("-")

    # Validate slug format
    if not _SLUG_PATTERN.match(slug):
        raise ValueError(
            f"Invalid slug '{slug}'. Must be 1-64 chars, lowercase alphanumeric with hyphens, no leading/trailing hyphens."
        )

    # Generate DID. Slug is globally unique (namespace isolation removed
    # 2026-08; daemon_id is recorded as ownership metadata but not part
    # of the identifier).
    did = f"did:agt:{slug}"

    # Check for duplicate globally
    existing = await db.execute(
        select(IdentityModel).where(IdentityModel.did == did)
    )
    if existing.scalar_one_or_none():
        raise ValueError(
            f"Identity with slug '{slug}' already exists (DID: {did})"
        )

    # Parse capabilities to JSON string
    capabilities = payload.get("capabilities", [])
    if isinstance(capabilities, list):
        capabilities_json = json.dumps(capabilities)
    else:
        capabilities_json = "[]"

    now = _utcnow_naive().isoformat()

    identity = IdentityModel(
        did=did,
        daemon_id=daemon_id,
        name=name,
        description=payload.get("description", ""),
        capabilities=capabilities_json,
        status="active",
        visibility=payload.get("visibility", "private"),
        created_by=payload.get("created_by", "user"),
        created_at=now,
        updated_at=now,
    )

    db.add(identity)
    await db.commit()
    await db.refresh(identity)

    # Write AGENTS.md file
    write_agent_md(identity, root_dir, force=False)

    log.info(f"Created identity: {did}")
    return did


async def get_identity(
    did: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Retrieve a single identity by DID.

    Args:
        did: The DID to look up
        db: Database session

    Returns:
        Identity dict or None if not found
    """
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return None

    return _identity_to_dict(identity)


async def list_identities(
    daemon_id: str | None = None,
    status: str | None = None,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """List identities with optional filtering.

    Args:
        daemon_id: Filter by daemon_id (None = all daemons)
        status: Filter by status (None = all statuses)
        db: Database session

    Returns:
        List of identity dicts, ordered by created_at descending
    """
    if db is None:
        raise ValueError("Database session required")

    stmt = select(IdentityModel).order_by(IdentityModel.created_at.desc())

    if daemon_id:
        stmt = stmt.where(IdentityModel.daemon_id == daemon_id)
    if status:
        stmt = stmt.where(IdentityModel.status == status)

    result = await db.execute(stmt)
    identities = result.scalars().all()
    return [_identity_to_dict(ident) for ident in identities]


async def list_identity_overviews(
    daemon_id: str | None = None,
    status: str | None = None,
    db: AsyncSession | None = None,
) -> list[dict[str, Any]]:
    """List identities with their bindings and runtime liveness in one pass.

    The settings UI needs both objects together. Returning a dedicated
    overview avoids the existing client-side ``1 + N`` binding requests.
    """
    if db is None:
        raise ValueError("Database session required")

    identities = await list_identities(
        daemon_id=daemon_id,
        status=status,
        db=db,
    )
    if not identities:
        return []

    dids = [identity["did"] for identity in identities]
    bindings = (
        await db.execute(
            select(IdentityBindingModel)
            .where(IdentityBindingModel.identity_did.in_(dids))
            .order_by(IdentityBindingModel.id.asc())
        )
    ).scalars().all()
    daemon_ids = {binding.daemon_id for binding in bindings}
    runtimes = []
    if daemon_ids:
        runtimes = (
            await db.execute(
                select(RuntimeModel).where(
                    RuntimeModel.daemon_id.in_(daemon_ids)
                )
            )
        ).scalars().all()

    runtime_by_pair = {
        (runtime.daemon_id, runtime.provider): runtime for runtime in runtimes
    }
    bindings_by_did: dict[str, list[dict[str, Any]]] = {}
    for binding in bindings:
        runtime = runtime_by_pair.get((binding.daemon_id, binding.provider))
        bindings_by_did.setdefault(binding.identity_did, []).append({
            "id": binding.id,
            "identity_did": binding.identity_did,
            "daemon_id": binding.daemon_id,
            "provider": binding.provider,
            "priority": binding.priority,
            "is_default": bool(binding.is_default),
            "created_at": binding.created_at,
            "runtime_online": (
                runtime is not None and not is_stale(runtime.last_seen)
            ),
            "last_seen": runtime.last_seen if runtime is not None else None,
        })

    return [
        {
            **identity,
            "bindings": bindings_by_did.get(identity["did"], []),
        }
        for identity in identities
    ]


async def update_identity(
    did: str,
    payload: dict[str, Any],
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Update an identity (partial update). Does NOT overwrite AGENTS.md file.

    Args:
        did: DID of the identity to update
        payload: IdentityUpdate payload with optional name, description, add_capabilities, remove_capabilities
        db: Database session

    Returns:
        Updated identity dict or None if not found
    """
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return None

    # Update fields if provided
    if payload.get("name") is not None:
        identity.name = payload["name"]
    if payload.get("description") is not None:
        identity.description = payload["description"]

    # Handle capabilities add/remove
    current_caps = json.loads(identity.capabilities) if identity.capabilities else []
    add_caps = payload.get("add_capabilities", [])
    remove_caps = payload.get("remove_capabilities", [])

    if add_caps or remove_caps:
        new_caps = [c for c in current_caps if c not in remove_caps]
        new_caps.extend(add_caps)
        # Remove duplicates while preserving order
        seen = set()
        unique_caps = []
        for cap in new_caps:
            if cap not in seen:
                unique_caps.append(cap)
                seen.add(cap)
        identity.capabilities = json.dumps(unique_caps)

    identity.updated_at = _utcnow_naive().isoformat()
    await db.commit()
    await db.refresh(identity)

    # Note: We do NOT overwrite AGENTS.md file on update to preserve user edits
    log.info(f"Updated identity: {did} (AGENTS.md file not overwritten)")

    return _identity_to_dict(identity)


async def deprecate_identity(
    did: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Soft-delete an identity by setting status to 'deprecated'.

    Args:
        did: DID of the identity to deprecate
        db: Database session

    Returns:
        Updated identity dict or None if not found
    """
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return None

    identity.status = "deprecated"
    identity.updated_at = _utcnow_naive().isoformat()
    await db.commit()
    await db.refresh(identity)

    log.info(f"Deprecated identity: {did}")
    return _identity_to_dict(identity)


async def revoke_identity(
    did: str,
    db: AsyncSession,
) -> dict[str, Any] | None:
    """Hard-revoke an identity by setting status to 'revoked'.

    Args:
        did: DID of the identity to revoke
        db: Database session

    Returns:
        Updated identity dict or None if not found
    """
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return None

    identity.status = "revoked"
    identity.updated_at = _utcnow_naive().isoformat()
    await db.commit()
    await db.refresh(identity)

    log.info(f"Revoked identity: {did}")
    return _identity_to_dict(identity)


async def delete_identity(
    did: str,
    db: AsyncSession,
    root_dir: Path | None = None,
) -> bool:
    """Physically delete an identity, but only if no tasks reference it.

    Also removes the identity's filesystem directory
    (``<root_dir>/.agt/identities/<slug>/``) so no AGENTS.md or other
    identity files are left behind.

    Args:
        did: DID of the identity to delete
        db: Database session
        root_dir: Root directory for AGENTS.md files (typically ~/.agt).
            When None, the file removal is skipped (defensive; callers
            should pass the same root they used at create time).

    Returns:
        True if deleted, False if reference exists (or not found)

    Raises:
        ValueError: If tasks reference this identity
    """
    from agt.server.models import StepModel

    # Check for task references (identity_did column)
    tasks_result = await db.execute(
        select(StepModel).where(StepModel.identity_did == did)
    )
    referencing_tasks = tasks_result.scalars().all()

    if referencing_tasks:
        task_ids = [t.step_id for t in referencing_tasks]
        raise ValueError(
            f"Cannot delete identity {did}: {len(task_ids)} task(s) still reference it: {', '.join(task_ids[:5])}"
            + (f"..." if len(task_ids) > 5 else "")
        )

    # No references, safe to delete
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return False

    await db.delete(identity)
    await db.commit()

    # Remove the identity's filesystem directory (AGENTS.md + anything
    # else under ~/.agt/identities/<slug>/). Tolerant of a missing dir —
    # identity may have been created with a different root or the file
    # was never written.
    if root_dir is not None:
        ident_dir = root_dir / ".agt" / "identities" / _dir_name(did)
        if ident_dir.exists():
            shutil.rmtree(ident_dir)
            log.info("Removed identity files: %s", ident_dir)

    log.info(f"Deleted identity: {did}")
    return True


async def sync_agent_md(
    did: str,
    db: AsyncSession,
    root_dir: Path,
) -> dict[str, Any] | None:
    """Force regenerate AGENTS.md file from current database state.

    Args:
        did: DID of the identity
        db: Database session
        root_dir: Root directory for AGENTS.md files (typically ~/.agt)

    Returns:
        Updated identity dict or None if not found
    """
    result = await db.execute(select(IdentityModel).where(IdentityModel.did == did))
    identity = result.scalar_one_or_none()
    if not identity:
        return None

    # Force overwrite AGENTS.md
    write_agent_md(identity, root_dir, force=True)

    log.info(f"Synced AGENTS.md for identity: {did}")
    return _identity_to_dict(identity)


def _identity_to_dict(identity: IdentityModel) -> dict[str, Any]:
    """Convert IdentityModel to dictionary for API responses.

    Args:
        identity: IdentityModel instance

    Returns:
        Dictionary with all identity fields
    """
    caps = json.loads(identity.capabilities) if identity.capabilities else []
    return {
        "did": identity.did,
        "daemon_id": identity.daemon_id,
        "name": identity.name,
        "description": identity.description,
        "capabilities": caps,
        "status": identity.status,
        "visibility": identity.visibility,
        "created_by": identity.created_by,
        "created_at": identity.created_at,
        "updated_at": identity.updated_at,
    }


__all__ = [
    "create_identity",
    "get_identity",
    "list_identities",
    "list_identity_overviews",
    "normalize_identity_did",
    "update_identity",
    "deprecate_identity",
    "revoke_identity",
    "delete_identity",
    "sync_agent_md",
    "write_agent_md",
]
