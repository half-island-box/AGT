"""SOP static-DAG service (Phase 5): expansion and dependency activation.

Expansion turns a validated :class:`SOPSpec` into a shared-root Job set:
no-dependency nodes activate immediately (Step materialized, claimable);
dependency nodes are created ``blocked`` and EMPTY — they enter the claim
pool only when :func:`activate_job` materializes their Step after every
upstream reached a terminal state.

Activation resolves ``${node.output}`` placeholders against the upstream
jobs' registered artifacts (mapping rule, review G3: ``ArtifactModel.name``
— which already folds the manifest-declared name — matched exactly against
the declared output name; multiple matches become a URI list; no match
means missing output → the node fails at activation and the failure
propagates to ITS dependents via ``try_wake_dependents``).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...a2a import DataPart, FilePart, Message, TextPart, render_message
from ..models import (
    ArtifactModel,
    IdentityBindingModel,
    IdentityModel,
    JobModel,
    RuntimeModel,
    TaskModel,
)
from .job_service import (
    _TERMINAL_STATUSES,
    _now,
    append_step,
    create_job,
    transition_job,
    try_wake_dependents,
)
from .runtime_service import is_stale
from .selection_service import route_runtime

log = logging.getLogger(__name__)

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z0-9_.-]+)\}")


@dataclass
class SOPExpansion:
    root_job_id: str
    node_jobs: dict[str, str] = field(default_factory=dict)


def _node_meta(node, identities, cwd=None, local_daemon_id=None) -> dict:
    identity_did = None
    provider_hint = node.provider
    if node.identity:
        entry = identities.get(node.identity)
        if entry is not None:
            identity_did = entry.did
            provider_hint = provider_hint or entry.provider
    return {
        "name": node.name,
        "input": node.input,
        "output": node.output,
        "identity_did": identity_did,
        "provider_hint": provider_hint,
        "explicit_provider": node.provider,
        "explicit_daemon": node.daemon,
        "dir": node.dir,
        "cwd": cwd,
        "local_daemon_id": local_daemon_id,
    }


async def _validate_explicit_targets(db: AsyncSession, spec) -> None:
    """Explicit (provider, daemon) pins must map to a registered runtime.

    Checked at expansion (not activation) so a typo'd daemon fails the
    submit immediately instead of leaving the node pending forever.
    """
    for node in spec.nodes:
        if not node.daemon:
            continue
        provider = node.provider
        if provider is None and node.identity:
            entry = spec.identities.get(node.identity)
            provider = entry.provider if entry else None
        if provider is None:
            raise ValueError({
                "type": "sop_explicit_provider_required",
                "message": f"task '{node.name}' pins a daemon but has no provider",
                "task": node.name,
            })
        row = (
            await db.execute(
                select(RuntimeModel).where(
                    RuntimeModel.daemon_id == node.daemon,
                    RuntimeModel.provider == provider,
                )
            )
        ).scalars().first()
        if row is None or is_stale(row.last_seen):
            raise ValueError({
                "type": "sop_daemon_not_live",
                "message": (
                    f"task '{node.name}' pins daemon '{node.daemon}' which has no "
                    f"live runtime for provider '{provider}'"
                ),
                "task": node.name,
            })


async def _validate_identities(db: AsyncSession, spec) -> None:
    """Identity ``did`` refs must exist, be active, and have a binding.

    Checked at expansion so a typo'd ``did`` (or a deleted/disowned
    identity) fails the submit immediately instead of parking the node
    pending forever. ``activate_job``'s unknown-identity check remains as
    a defensive backstop (an identity could be deleted between submit and
    activation).
    """
    for alias, entry in spec.identities.items():
        if not entry.did:
            continue
        ident = await db.get(IdentityModel, entry.did)
        if ident is None:
            raise ValueError({
                "type": "sop_identity_not_found",
                "message": f"identity '{alias}' references unknown did '{entry.did}'",
                "identity": alias,
                "did": entry.did,
            })
        if ident.status != "active":
            raise ValueError({
                "type": "sop_identity_not_active",
                "message": f"identity '{alias}' (did '{entry.did}') is {ident.status}",
                "identity": alias,
                "did": entry.did,
                "status": ident.status,
            })
        binding = (
            await db.execute(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.identity_did == entry.did
                )
            )
        ).scalars().first()
        if binding is None:
            raise ValueError({
                "type": "sop_identity_no_bindings",
                "message": f"identity '{alias}' (did '{entry.did}') has no bindings",
                "identity": alias,
                "did": entry.did,
            })


async def _intended_providers(
    db: AsyncSession, node, identities: dict
) -> set[str]:
    """The provider set a node's routing must cover (liveness pre-check).

    Resolution order: explicit ``provider`` > identity ``did``'s bindings >
    identity alias ``provider``. Empty = no provider intent (the ``claude``
    default applies at expansion; activation's fail-fast guard covers that
    no-intent case).
    """
    if node.provider:
        return {node.provider}
    if not node.identity:
        return set()
    entry = identities.get(node.identity)
    if entry is None:
        return set()
    if entry.did:
        bindings = (
            await db.execute(
                select(IdentityBindingModel).where(
                    IdentityBindingModel.identity_did == entry.did
                )
            )
        ).scalars().all()
        return {b.provider for b in bindings}
    if entry.provider:
        return {entry.provider}
    return set()


async def _live_provider_set(db: AsyncSession, providers: set[str]) -> set[str]:
    """The subset of ``providers`` that has a non-stale runtime row."""
    if not providers:
        return set()
    rows = (
        await db.execute(
            select(RuntimeModel).where(RuntimeModel.provider.in_(providers))
        )
    ).scalars().all()
    return {r.provider for r in rows if not is_stale(r.last_seen)}


async def _validate_routability(db: AsyncSession, spec) -> None:
    """A node's intended provider(s) must have at least one live runtime.

    Catches the "provider is declared but every runtime offering it is
    offline/stale" case at submit — which would otherwise expand into a
    free task no daemon can ever claim. ``activate_job``'s fail-fast guard
    is the backstop for any residual routing miss.
    """
    for node in spec.nodes:
        if node.daemon:
            continue  # daemon pins validated by _validate_explicit_targets
        providers = await _intended_providers(db, node, spec.identities)
        if not providers:
            continue
        if not await _live_provider_set(db, providers):
            raise ValueError({
                "type": "sop_provider_not_live",
                "message": (
                    f"task '{node.name}' requires provider(s) "
                    f"{sorted(providers)} but none has a live runtime"
                ),
                "task": node.name,
                "providers": sorted(providers),
            })


async def expand_sop(
    db: AsyncSession, spec, cwd: str | None = None, local_daemon_id: str | None = None
) -> SOPExpansion:
    """Materialize a validated SOPSpec into shared-root Jobs.

    All nodes start as blocked empty Jobs carrying their activation
    metadata (``sop_node``); no-dependency nodes are activated right
    away, so their end state matches the spec: ``queued`` + one pending
    Step. The FIRST no-dependency node's job_id is the shared root
    (design D3) — created first so every later node can override
    ``root_job_id`` (review G2: SOP nodes have no spawn parent, the
    default root=self would silently break the shared-root invariant).

    ``cwd`` / ``local_daemon_id`` carry the submitter's pwd and local
    daemon id so ``activate_job`` can default an un-pinned node's
    ``agent_cwd`` to pwd when it lands on the local daemon (and refuse a
    remote landing with no ``dir``).
    """
    await _validate_explicit_targets(db, spec)
    await _validate_identities(db, spec)
    await _validate_routability(db, spec)

    no_deps = [n for n in spec.nodes if not n.depends_on]
    if not no_deps:
        raise ValueError({"type": "sop_no_entry", "message": "DAG has no entry node"})

    root_node = no_deps[0]
    root = await create_job(
        db,
        provider=root_node.provider
        or (spec.identities.get(root_node.identity).provider
            if root_node.identity and spec.identities.get(root_node.identity)
            else None)
        or "claude",
        prompt="",
        identity_did=(spec.identities.get(root_node.identity).did
                      if root_node.identity and spec.identities.get(root_node.identity)
                      else None),
        with_step=False,
        sop_node=json.dumps(
            _node_meta(root_node, spec.identities, cwd, local_daemon_id),
            ensure_ascii=False,
        ),
    )
    expansion = SOPExpansion(root_job_id=root.job_id)
    expansion.node_jobs[root_node.name] = root.job_id

    for node in spec.nodes:
        if node.name == root_node.name:
            continue
        meta = _node_meta(node, spec.identities, cwd, local_daemon_id)
        job = await create_job(
            db,
            provider=meta["provider_hint"] or "claude",
            prompt="",
            identity_did=meta["identity_did"],
            with_step=False,
            parents=[expansion.node_jobs[d] for d in node.depends_on],
            root_job_id=root.job_id,
            sop_node=json.dumps(meta, ensure_ascii=False),
        )
        expansion.node_jobs[node.name] = job.job_id

    # Entry nodes are runnable now — uniform activation path.
    for node in no_deps:
        await activate_job(db, await _by_id(db, expansion.node_jobs[node.name]))
    return expansion


async def _by_id(db: AsyncSession, job_id: str) -> JobModel:
    job = await db.get(JobModel, job_id)
    if job is None:  # pragma: no cover — internal invariant
        raise ValueError({"type": "job_not_found", "message": job_id})
    return job


async def _upstream_jobs(db: AsyncSession, job: JobModel) -> list[JobModel]:
    from ..models import JobParentModel

    rows = (
        await db.execute(
            select(JobParentModel.parent_job_id).where(JobParentModel.job_id == job.job_id)
        )
    ).scalars().all()
    out = []
    for pid in rows:
        parent = await db.get(JobModel, pid)
        if parent is not None:
            out.append(parent)
    return out


async def _resolve_placeholders(
    db: AsyncSession, job: JobModel, meta: dict, upstreams: list[JobModel]
) -> tuple[str, list[str]]:
    """Resolve ``${node.output}`` → artifact URIs.

    Returns ``(resolved_input, uris)``. Raises ``LookupError`` with the
    reason when an output is missing (caller fails the node at birth).
    Mapping rule (G3): an upstream artifact matches when its ``name``
    equals the declared output name — the scan stage already folds the
    manifest-declared name into ``ArtifactModel.name``, so manifest
    naming wins naturally and the bare filename is the fallback.
    """
    from ...a2a import make_artifact_uri

    text: str = meta.get("input") or ""
    replacements: dict[str, str] = {}
    uris: list[str] = []

    for ph in _PLACEHOLDER_RE.findall(text):
        if ph in replacements:
            continue
        node_name, _, output_name = ph.partition(".")
        matched: list[ArtifactModel] = []
        for up in upstreams:
            up_meta = json.loads(up.sop_node) if up.sop_node else {}
            if up_meta.get("name") != node_name:
                continue
            arts = await _artifacts_of(db, up.job_id)
            matched.extend(a for a in arts if a.name == output_name)
        if not matched:
            raise LookupError(
                f"upstream '{node_name}' produced no artifact named '{output_name}'"
            )
        found = [make_artifact_uri(a.artifact_id) for a in matched]
        uris.extend(found)
        replacements[ph] = found[0] if len(found) == 1 else "[" + ", ".join(found) + "]"

    resolved = _PLACEHOLDER_RE.sub(lambda m: replacements[m.group(1)], text)
    return resolved, uris


async def _artifacts_of(db: AsyncSession, job_id: str) -> list[ArtifactModel]:
    return (
        await db.execute(
            select(ArtifactModel).where(ArtifactModel.job_id == job_id)
        )
    ).scalars().all()


async def activate_job(db: AsyncSession, job: JobModel) -> JobModel:
    """Activate a blocked SOP node: route, resolve refs, materialize Step.

    Called from :func:`job_service.try_wake_dependents` once every
    upstream is terminal (and directly from ``expand_sop`` for entry
    nodes). Missing upstream output / unresolvable identity → the node
    FAILS at activation (a terminal Step carrying the reason) and the
    failure wakes its own dependents (chain propagation, review G1).
    """
    meta = json.loads(job.sop_node) if job.sop_node else {}
    upstreams = await _upstream_jobs(db, job)

    # Route: explicit pin wins (validated at expand); otherwise dynamic.
    provider = meta.get("explicit_provider")
    target = meta.get("explicit_daemon")
    if provider is None or target is None:
        routed = await route_runtime(
            db,
            identity_did=meta.get("identity_did"),
            provider_hint=meta.get("provider_hint"),
            requested_provider=meta.get("explicit_provider"),
            requested_daemon_id=meta.get("explicit_daemon"),
        )
        if routed is not None:
            provider = routed.provider
            target = routed.daemon_id
        else:
            # No live runtime for the node's intended provider(s). A SOP
            # node must resolve to a concrete daemon+provider or fail — a
            # free task here would sit in the claim pool forever (the
            # submit-time _validate_routability/liveness check should have
            # caught this, but a daemon can go stale between submit and
            # activation; this is the backstop).
            failed_provider = (
                meta.get("explicit_provider") or meta.get("provider_hint")
            )
            reason = (
                f"no live runtime for provider '{failed_provider}'"
                if failed_provider
                else "no provider/identity specified and no live default runtime"
            )
            return await _fail_at_activation(db, job, provider, target, reason)

    # Identity two-state (D13 alignment): explicit-but-unknown → fail.
    identity_did = meta.get("identity_did")
    if identity_did and await db.get(IdentityModel, identity_did) is None:
        return await _fail_at_activation(
            db, job, provider, target, f"unknown identity: {identity_did}"
        )

    # Resolve agent_cwd (dir precedence): explicit task.dir wins; else the
    # submitter's pwd when the node lands on the local daemon; else fail —
    # a remote daemon can't see the local cwd, so a remote landing without
    # ``dir`` is a user error surfaced here rather than a silent fallback.
    # When no local context was submitted (local_daemon_id is None, e.g. an
    # API submit), leave agent_cwd None and let the daemon pick its sandbox.
    explicit_dir = meta.get("dir")
    if explicit_dir:
        job.agent_cwd = explicit_dir
    elif meta.get("local_daemon_id") is not None:
        if target and target == meta.get("local_daemon_id"):
            job.agent_cwd = meta.get("cwd")
        else:
            return await _fail_at_activation(
                db, job, provider, target,
                f"node routed to daemon '{target}' with no 'dir' and it is "
                f"not the local daemon; add `dir: <absolute path the daemon "
                f"can reach>` (or pin `daemon:` to the local daemon "
                f"'{meta.get('local_daemon_id')}')",
            )

    try:
        resolved_input, uris = await _resolve_placeholders(db, job, meta, upstreams)
    except LookupError as exc:
        return await _fail_at_activation(db, job, provider, target, str(exc))

    # Output contract: when a node declares ``output``, tell the agent the
    # exact deliverable path. ``${node.output}`` placeholders downstream
    # resolve by matching ``ArtifactModel.name == output``, and the agent
    # has no other way to learn that required filename — without this the
    # node would "succeed" by printing to stdout and its downstream would
    # fail at activation ("no artifact named ...").
    output_name = meta.get("output")
    if output_name:
        resolved_input = (
            f"{resolved_input}\n\n"
            f"[agt] Write this task's final deliverable to "
            f"'.agt/outputs/{output_name}'."
        )

    parts: list = [TextPart(text=resolved_input)]
    if uris:
        parts.append(DataPart(data={"inputs": uris}))
        for uri in uris:
            parts.append(FilePart(uri=uri))
    message = Message(role="user", parts=parts)

    # Provider writeback (review G4): append_step reads job.provider.
    job.provider = provider or job.provider
    job.target_daemon_id = target
    job.updated_at = _now()
    await transition_job(db, job.job_id, "dependencies_satisfied")
    await append_step(
        db,
        job.job_id,
        render_message(message),
        message=message,
        target_daemon_id=target,
    )
    return job


async def _fail_at_activation(
    db: AsyncSession, job: JobModel, provider, target, reason: str
) -> JobModel:
    """Terminal-fail a node at activation (no subprocess, no claim)."""
    job.provider = provider or job.provider
    job.target_daemon_id = target
    job.updated_at = _now()
    await transition_job(db, job.job_id, "dependencies_satisfied")
    step = await append_step(
        db,
        job.job_id,
        f"[sop] activation failed: {reason}",
        target_daemon_id=target,
    )
    step.status = "failed"
    step.outcome = "error"
    step.stderr = f"sop activation failed: {reason}"
    step.exit_code = -1
    step.updated_at = _now()
    await transition_job(db, job.job_id, "step_failed")
    # Chain propagation (review G1): this terminal failure wakes dependents.
    await try_wake_dependents(db, job.job_id)
    return job


__all__ = ["SOPExpansion", "expand_sop", "activate_job"]
