"""Selection service — pick a runtime for an identity (Phase 4 Step 2).

Implements the algorithm from phase4-design.md §5.1:

1. Validate the identity (exists, status == active).
2. Load its bindings.
3. If the caller named a runtime (``runtime_hint``), filter to it.
4. Otherwise prefer ``is_default``, then highest ``priority``, with a
   stable id-ascending tie-break.

The caller (Step 4's submit_task) decides whether to call this at all:
``identity_did=None`` means "no identity path" and must bypass selection
entirely — there is deliberately no daemon-level default identity
(design doc §12.1).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from agt.server.models import IdentityBindingModel, IdentityModel

log = logging.getLogger(__name__)


@dataclass
class SelectionResult:
    """The runtime a task with this identity should run on."""

    daemon_id: str
    provider: str
    identity_did: str


class SelectionError(ValueError):
    """Selection failure with a machine-readable type and context.

    Inherits ValueError so the routes layer's existing
    ``except ValueError`` translation keeps working; ``err_type`` and
    ``context`` carry the structured details for HTTP mapping and CLI
    hints.
    """

    def __init__(self, err_type: str, message: str, **context) -> None:
        self.err_type = err_type
        self.context = context
        super().__init__(message)


async def select_runtime(
    identity_did: str,
    runtime_hint: str | None,
    daemon_id: str,
    db: AsyncSession,
) -> SelectionResult:
    """Resolve ``(identity_did, runtime_hint)`` to a concrete runtime.

    Args:
        identity_did: The user-specified identity (required — callers
            handle the None case before reaching here).
        runtime_hint: Optional provider name the user explicitly named.
        daemon_id: The requesting daemon (accepted for interface
            compatibility; bindings are daemon-scoped already).
        db: Database session.

    Returns:
        SelectionResult with the chosen daemon_id / provider / identity_did.

    Raises:
        SelectionError: one of identity_not_found / identity_not_active /
            no_bindings / no_binding_for_runtime.
    """
    # Step 1: identity must exist and be active.
    identity = await db.get(IdentityModel, identity_did)
    if identity is None:
        raise SelectionError(
            "identity_not_found",
            f"identity '{identity_did}' not found",
            did=identity_did,
        )
    if identity.status != "active":
        raise SelectionError(
            "identity_not_active",
            f"identity '{identity_did}' is {identity.status}",
            did=identity_did,
            status=identity.status,
        )

    # Step 2: load bindings (id order keeps every later choice stable).
    result = await db.execute(
        select(IdentityBindingModel)
        .where(IdentityBindingModel.identity_did == identity_did)
        .order_by(IdentityBindingModel.id.asc())
    )
    bindings = list(result.scalars().all())
    if not bindings:
        raise SelectionError(
            "no_bindings",
            f"identity '{identity_did}' has no bindings. "
            f"Use 'agt identity bind' first",
            did=identity_did,
        )

    # Step 3: an explicit runtime hint is a filter, not a preference.
    if runtime_hint is not None:
        matching = [b for b in bindings if b.provider == runtime_hint]
        if not matching:
            available = sorted({b.provider for b in bindings})
            raise SelectionError(
                "no_binding_for_runtime",
                f"identity '{identity_did}' has no binding for "
                f"'{runtime_hint}'. Available: {', '.join(available)}",
                did=identity_did,
                runtime=runtime_hint,
                available=available,
            )
        best = matching[0]
        return SelectionResult(
            daemon_id=best.daemon_id,
            provider=best.provider,
            identity_did=identity_did,
        )

    # Step 4: no hint — default flag first, then priority, then id order.
    defaults = [b for b in bindings if b.is_default]
    if defaults:
        best = defaults[0]
    else:
        best = max(bindings, key=lambda b: b.priority)
    return SelectionResult(
        daemon_id=best.daemon_id,
        provider=best.provider,
        identity_did=identity_did,
    )


__all__ = ["SelectionResult", "SelectionError", "select_runtime"]
