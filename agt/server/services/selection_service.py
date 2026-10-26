"""Selection service — pick a runtime for an identity.

Bindings are routing preferences, not an allow-list. An identity may run
on any live runtime selected by the caller; a missing or mismatched
binding must not make the identity unusable with another provider.

Selection rules:

1. Validate the identity (exists, status == active).
2. Prefer bindings matching any explicit daemon/provider constraints.
3. Prefer live bindings, then ``is_default``, then highest ``priority``,
   with a stable id-ascending tie-break.
4. Fall back to the live runtime registry when no binding matches.
5. If no runtime is live but a matching binding exists, return that
   binding so the caller's existing fail-fast liveness check can produce
   the authoritative error.

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

from agt.server.models import IdentityBindingModel, IdentityModel, RuntimeModel
from agt.server.models import StepModel

from .runtime_service import is_stale

log = logging.getLogger(__name__)

# Phase 4 dynamic routing: how many recent tasks with the same identity
# count toward daemon affinity.
AFFINITY_WINDOW = 20


@dataclass
class SelectionResult:
    """The runtime a task with this identity should run on."""

    daemon_id: str
    provider: str
    identity_did: str


@dataclass
class RoutingDecision:
    """Phase 4 dynamic-routing result: daemon AND provider jointly.

    Returning both is load-bearing (review fix #2): ``spawn_children``
    uses the routed provider to override its local single-value
    resolution, so the step's target daemon always has a runtime for
    the step's provider — a mismatch would leave it queued forever.
    """

    daemon_id: str
    provider: str


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


async def _live_runtime_pairs(
    db: AsyncSession,
    bindings: list[IdentityBindingModel],
) -> set[tuple[str, str]]:
    """Return the live ``(daemon_id, provider)`` pairs covering ``bindings``.

    A pair is live when the daemon has a runtime row for that provider
    whose ``last_seen`` is within the liveness TTL. Daemons with no rows
    (never registered / pruned) are not live.
    """
    daemon_ids = {b.daemon_id for b in bindings}
    if not daemon_ids:
        return set()
    rows = (
        await db.execute(
            select(RuntimeModel).where(RuntimeModel.daemon_id.in_(daemon_ids))
        )
    ).scalars().all()
    return {
        (row.daemon_id, row.provider) for row in rows if not is_stale(row.last_seen)
    }


async def _registry_runtime(
    db: AsyncSession,
    *,
    provider: str | None,
    daemon_id: str | None,
) -> tuple[str, str] | None:
    """Pick a runtime directly from the registry.

    This is the zero-binding fallback and also the escape hatch when an
    identity has bindings, but none match the caller's explicit target.
    Live runtimes win. When the caller supplied a hard daemon/provider
    target, a stale row may still be returned: submit-time validation
    owns the liveness rejection and should report the concrete target.
    """
    stmt = select(RuntimeModel)
    if provider is not None:
        stmt = stmt.where(RuntimeModel.provider == provider)
    if daemon_id is not None:
        stmt = stmt.where(RuntimeModel.daemon_id == daemon_id)
    rows = (await db.execute(stmt)).scalars().all()
    if not rows:
        return None

    live = [row for row in rows if not is_stale(row.last_seen)]
    explicit = provider is not None or daemon_id is not None
    pool = live or (rows if explicit else [])
    if not pool:
        return None

    # Stable fallback when bindings provide no ordering signal.
    best = min(pool, key=lambda row: (row.daemon_id, row.provider, row.id))
    return best.daemon_id, best.provider


def _pick_binding(
    pool: list[IdentityBindingModel],
    live_pairs: set[tuple[str, str]],
    *,
    provider: str | None,
) -> IdentityBindingModel:
    """Pick the best binding from a non-empty, id-ascending pool.

    Preference order:
    1. Live daemons — the binding's ``(daemon_id, provider)`` has a
       fresh runtime row. With a hint, the provider is the hint itself;
       without one, each binding's own provider. Falls back to the whole
       pool when nothing is live (the submit-time live-runtime check
       then rejects with a clear 400).
    2. ``is_default`` first.
    3. Highest ``priority`` (``max`` keeps the first max in id order —
       stable tie-break).
    """
    def _is_live(b: IdentityBindingModel) -> bool:
        prov = provider if provider is not None else b.provider
        return (b.daemon_id, prov) in live_pairs

    live_pool = [b for b in pool if _is_live(b)] or pool
    defaults = [b for b in live_pool if b.is_default]
    if defaults:
        return defaults[0]
    return max(live_pool, key=lambda b: b.priority)


async def select_runtime(
    identity_did: str,
    runtime_hint: str | None,
    target_daemon_id: str | None,
    db: AsyncSession,
) -> SelectionResult:
    """Resolve ``(identity_did, runtime_hint)`` to a concrete runtime.

    Explicit daemon/provider values are hard constraints. Bindings narrow
    and rank the candidates when they match, but never gate identity use:
    the runtime registry remains a fallback.

    Args:
        identity_did: The user-specified identity (required — callers
            handle the None case before reaching here).
        runtime_hint: Optional provider name the user explicitly named.
        target_daemon_id: Optional daemon the caller explicitly selected.
        db: Database session.

    Returns:
        SelectionResult with the chosen daemon_id / provider / identity_did.

    Raises:
        SelectionError: identity_not_found / identity_not_active, or
            no_live_runtime when no matching binding/runtime exists.
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

    # Step 2: load bindings as preferences (id order keeps choices stable).
    result = await db.execute(
        select(IdentityBindingModel)
        .where(IdentityBindingModel.identity_did == identity_did)
        .order_by(IdentityBindingModel.id.asc())
    )
    bindings = list(result.scalars().all())
    matching = [
        binding
        for binding in bindings
        if (
            (runtime_hint is None or binding.provider == runtime_hint)
            and (
                target_daemon_id is None
                or binding.daemon_id == target_daemon_id
            )
        )
    ]

    # Step 3: if any matching bindings are live, their default/priority
    # ordering is the intended routing preference.
    live_pairs = await _live_runtime_pairs(db, bindings)
    live_matching = [
        binding
        for binding in matching
        if (binding.daemon_id, binding.provider) in live_pairs
    ]
    if live_matching:
        best = _pick_binding(
            live_matching,
            live_pairs,
            provider=runtime_hint,
        )
        return SelectionResult(
            daemon_id=best.daemon_id,
            provider=best.provider,
            identity_did=identity_did,
        )

    # Step 4: no live matching binding. A live unbound runtime is valid:
    # this is what makes bindings optional and prevents a stale binding
    # from blocking an otherwise healthy provider.
    registry = await _registry_runtime(
        db,
        provider=runtime_hint,
        daemon_id=target_daemon_id,
    )
    if registry is not None:
        daemon_id, provider = registry
        return SelectionResult(
            daemon_id=daemon_id,
            provider=provider,
            identity_did=identity_did,
        )

    # Step 5: preserve the old dry-run behavior for configured bindings
    # whose runtime is offline. Submit callers still reject them via the
    # live-runtime check with a concrete daemon/provider error.
    if matching:
        best = _pick_binding(
            matching,
            live_pairs,
            provider=runtime_hint,
        )
        return SelectionResult(
            daemon_id=best.daemon_id,
            provider=best.provider,
            identity_did=identity_did,
        )

    raise SelectionError(
        "no_live_runtime",
        (
            f"no runtime matches identity '{identity_did}'"
            + (f" for provider '{runtime_hint}'" if runtime_hint else "")
            + (f" on daemon '{target_daemon_id}'" if target_daemon_id else "")
        ),
        did=identity_did,
        runtime=runtime_hint,
        daemon_id=target_daemon_id,
        available=sorted({b.provider for b in bindings}),
    )


# ── Phase 4: dynamic routing (spawn/sessionless path) ───────────────


async def route_runtime(
    db: AsyncSession,
    *,
    identity_did: str | None,
    provider_hint: str | None,
    requested_daemon_id: str | None = None,
    requested_provider: str | None = None,
) -> "RoutingDecision | None":
    """Jointly route ``(daemon_id, provider)`` by capability + scoring.

    Unlike :func:`select_runtime` (submit/sessionful path — binding-
    gated, resolve-once), this function treats bindings as *hints*:
    an identity with zero bindings still routes to any live daemon
    whose runtime covers a candidate provider.

    ``requested_daemon_id`` / ``requested_provider`` are phase-A *hard*
    placement constraints from a parent's spawn envelope: when set they
    narrow the candidate set, and when the request can't be honored
    (daemon offline / doesn't offer the provider) the function returns
    ``None`` so the caller can fail the child at birth rather than
    silently rerouting. Neither gate is binding-scoped — a parent may
    pin a child to a daemon/provider its identity isn't bound to, as
    long as that daemon has a live runtime for the provider.

    Candidates: ``(provider, daemon)`` pairs where provider ∈ the
    candidate provider set (requested provider, else identity's bindings
    dedup, else ``{provider_hint}``) and the daemon has a runtime row for
    it that is not stale. "Online" is uniformly ``!is_stale(last_seen)`` —
    ``RuntimeModel.status`` is display-only.

    Scoring per daemon: affinity (recent N=20 tasks with the same
    identity on that daemon) − load (running-Step count, i.e.
    claimed-but-unreported). Tiebreak: binding priority, then stable
    daemon id. Returns None when no candidate exists (caller decides the
    fallback — spawn_children falls back to the parent daemon on a plain
    miss, but fails the child at birth when placement was requested).
    """
    # Bindings are read once: they drive the candidate provider set (when
    # no provider was requested) AND the priority tiebreak below.
    bind_rows: list[IdentityBindingModel] = []
    if identity_did:
        bind_rows = (
            await db.execute(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.identity_did == identity_did
                )
            )
        ).scalars().all()

    # 1. Candidate provider set: an explicit requested provider (hard) >
    #    identity bindings' providers (dedup) > the provider hint
    #    (identity-less / zero-binding fallback).
    if requested_provider is not None:
        candidate_providers = {requested_provider}
    elif identity_did:
        candidate_providers = {b.provider for b in bind_rows}
        if not candidate_providers:
            candidate_providers = {provider_hint} if provider_hint else set()
    else:
        candidate_providers = {provider_hint} if provider_hint else set()
    if not candidate_providers:
        return None

    # 2. Live (provider, daemon) pairs from RuntimeModel.
    runtime_rows = (
        await db.execute(
            select(RuntimeModel).where(RuntimeModel.provider.in_(candidate_providers))
        )
    ).scalars().all()
    offered: dict[str, set[str]] = {}
    for row in runtime_rows:
        if not is_stale(row.last_seen):
            offered.setdefault(row.daemon_id, set()).add(row.provider)
    if not offered:
        return None

    # 2b. A requested daemon is a hard constraint: narrow ``offered`` to
    #     just that daemon; if it has no live runtime for a candidate
    #     provider, the request is un-honorable → None.
    if requested_daemon_id is not None:
        if requested_daemon_id not in offered:
            return None
        offered = {requested_daemon_id: offered[requested_daemon_id]}

    daemon_ids = list(offered.keys())

    # 3. Affinity: recent tasks with this identity, counted per daemon.
    affinity: dict[str, int] = {}
    if identity_did:
        recent = (
            await db.execute(
                select(StepModel.daemon_id)
                .where(StepModel.identity_did == identity_did)
                .order_by(StepModel.updated_at.desc())
                .limit(AFFINITY_WINDOW)
            )
        ).scalars().all()
        for did in recent:
            if did:
                affinity[did] = affinity.get(did, 0) + 1

    # 4. Load: running (claimed-but-unreported) steps per daemon.
    #    Missing rows count as 0 — a signal gap must never fail routing.
    load: dict[str, int] = {}
    running_rows = (
        await db.execute(
            select(StepModel.daemon_id)
            .where(StepModel.status == "running")
            .where(StepModel.daemon_id.in_(daemon_ids))
        )
    ).scalars().all()
    for did in running_rows:
        load[did] = load.get(did, 0) + 1

    # 5. Binding priority as tiebreak hint (identity-scoped, max per daemon).
    binding_priority: dict[str, int] = {}
    if identity_did:
        for b in bind_rows:
            cur = binding_priority.get(b.daemon_id)
            if cur is None or b.priority > cur:
                binding_priority[b.daemon_id] = b.priority

    # Descending score / priority, ascending daemon id for stability:
    # min over the negated keys == max with stable final tiebreak.
    best_daemon = min(
        daemon_ids,
        key=lambda d: (
            -(affinity.get(d, 0) - load.get(d, 0)),
            -binding_priority.get(d, -1),
            d,
        ),
    )

    # 6. Provider within the chosen daemon: binding priority first among
    #    the providers it actually offers, else stable sorted pick.
    available_providers = offered[best_daemon]
    chosen_provider = sorted(available_providers)[0]
    if identity_did:
        for b in sorted(bind_rows, key=lambda b: (-b.priority, b.id)):
            if b.provider in available_providers:
                chosen_provider = b.provider
                break

    return RoutingDecision(daemon_id=best_daemon, provider=chosen_provider)


__all__ = [
    "SelectionResult",
    "SelectionError",
    "select_runtime",
    "RoutingDecision",
    "route_runtime",
]
