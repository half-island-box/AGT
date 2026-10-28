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
    JobParentModel,
    RuntimeModel,
    StepModel,
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
    loop_node_jobs: dict[str, dict[int, str]] = field(default_factory=dict)
    sentinel_jobs: dict[str, str] = field(default_factory=dict)


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


def _loop_meta_json(loop) -> str:
    return json.dumps(
        {"name": loop.name, "max_iterations": loop.max_iterations, "entry": loop.entry},
        ensure_ascii=False,
    )


async def _create_loop_sentinel(
    db: AsyncSession, loop, root_job_id: str, cwd: str | None = None
) -> str:
    """Create a blocked sentinel job that represents loop completion.

    The sentinel has no ``sop_node`` so it does not show up as a DAG node
    in status queries; loop metadata lives in ``sop_loop_*`` columns. It
    becomes terminal after the final iteration, waking external dependents.
    """
    job = await create_job(
        db,
        provider="claude",
        prompt="",
        with_step=False,
        root_job_id=root_job_id,
        work_dir=cwd,
        job_type="sop",
        sop_loop_name=loop.name,
        sop_iteration=0,
        sop_loop_meta=_loop_meta_json(loop),
    )
    return job.job_id


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
            if provider is None and entry is not None and entry.did:
                # Bindings are optional, but when one exists on the pinned
                # daemon it can provide the missing provider hint.
                binding_providers = (
                    await db.execute(
                        select(IdentityBindingModel.provider)
                        .where(
                            IdentityBindingModel.identity_did == entry.did,
                            IdentityBindingModel.daemon_id == node.daemon,
                        )
                        .distinct()
                    )
                ).scalars().all()
                if len(binding_providers) == 1:
                    provider = binding_providers[0]
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
    """Identity ``did`` refs must exist and be active.

    Checked at expansion so a typo'd ``did`` (or a deleted/disowned
    identity) fails the submit immediately instead of parking the node
    pending forever. ``activate_job``'s unknown-identity check remains as
    a defensive backstop (an identity could be deleted between submit and
    activation). Bindings are routing preferences and are deliberately
    not required.
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
            if node.identity:
                entry = spec.identities.get(node.identity)
                if entry is not None and entry.did and not entry.provider:
                    raise ValueError({
                        "type": "sop_identity_provider_required",
                        "message": (
                            f"task '{node.name}' uses identity '{node.identity}' "
                            "without a provider or matching binding; add "
                            "'provider' to the task or identity alias"
                        ),
                        "task": node.name,
                        "identity": node.identity,
                        "did": entry.did,
                    })
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


def _topo_sort_non_loop(nodes: list) -> list:
    """Stably topologically sort non-loop nodes by non-loop dependency edges.

    ``parse_sop`` rejects cycles outside a declared loop, so the non-loop
    subgraph is acyclic and a dependent can always be placed after its
    non-loop upstreams — regardless of the author's ``tasks`` order.
    """
    by_name = {n.name: n for n in nodes}
    indeg = {n.name: 0 for n in nodes}
    dependents: dict[str, list[str]] = {n.name: [] for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            if dep in by_name:  # non-loop -> non-loop edge only
                indeg[n.name] += 1
                dependents[dep].append(n.name)

    queue = [n.name for n in nodes if indeg[n.name] == 0]
    ordered: list = []
    while queue:
        current = queue.pop(0)  # FIFO keeps the stable spec order for ties
        ordered.append(by_name[current])
        for dependent in dependents[current]:
            indeg[dependent] -= 1
            if indeg[dependent] == 0:
                queue.append(dependent)
    return ordered


def _resolve_parent(expansion: SOPExpansion, node_by_name: dict, node, dep: str) -> str:
    """Resolve one ``depends_on`` name to an already-materialized job id.

    Raises a structured ``ValueError`` (mapped to 400 by the route) instead
    of a bare ``KeyError`` when a parent is missing — a defensive backstop
    for any residual ordering bug during expansion.
    """
    dep_node = node_by_name[dep]
    if node.loop_name is not None and dep_node.loop_name == node.loop_name:
        job_id = expansion.loop_node_jobs.get(dep_node.name, {}).get(1)
    elif dep_node.loop_name:
        job_id = expansion.sentinel_jobs.get(dep_node.loop_name)
    else:
        job_id = expansion.node_jobs.get(dep)
    if job_id is None:
        raise ValueError({
            "type": "sop_internal_parent_missing",
            "message": f"task '{node.name}' depends on unresolved task '{dep}'",
            "task": node.name,
            "dep": dep,
        })
    return job_id


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

    Phase 5 loops: nodes inside a declared ``loops:`` block are expanded
    into one job per iteration. Non-loop nodes depend on loop sentinels
    instead of individual iteration jobs; loop-internal edges stay inside
    the same iteration.

    ``cwd`` / ``local_daemon_id`` carry the submitter's pwd and local
    daemon id so ``activate_job`` can default an un-pinned node's
    ``agent_cwd`` to pwd when it lands on the local daemon (and refuse a
    remote landing with no ``dir``).
    """
    await _validate_explicit_targets(db, spec)
    await _validate_identities(db, spec)
    await _validate_routability(db, spec)

    node_by_name = {n.name: n for n in spec.nodes}
    loop_by_name = {l.name: l for l in spec.loops}

    non_loop_nodes = [n for n in spec.nodes if not n.loop_name]
    loop_nodes = [n for n in spec.nodes if n.loop_name]

    # Determine root: prefer a non-loop no-dep node, else the first loop entry.
    no_dep_non_loop = [n for n in non_loop_nodes if not n.depends_on]
    root_node: SOPNode
    if no_dep_non_loop:
        root_node = no_dep_non_loop[0]
    elif spec.loops:
        root_node = node_by_name[spec.loops[0].entry]
    else:
        raise ValueError({"type": "sop_no_entry", "message": "DAG has no entry node"})

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
        work_dir=cwd,
        job_type="sop",
        sop_node=json.dumps(
            _node_meta(root_node, spec.identities, cwd, local_daemon_id),
            ensure_ascii=False,
        ),
    )
    expansion = SOPExpansion(root_job_id=root.job_id)

    # If the root is itself inside a loop, stamp loop metadata on it.
    if root_node.loop_name:
        loop = loop_by_name[root_node.loop_name]
        root.sop_loop_name = loop.name
        root.sop_iteration = 1
        root.sop_loop_meta = _loop_meta_json(loop)
        root_meta = json.loads(root.sop_node) if root.sop_node else {}
        root_meta["loop_name"] = loop.name
        root_meta["iteration"] = 1
        root.sop_node = json.dumps(root_meta, ensure_ascii=False)
        expansion.loop_node_jobs[root_node.name] = {1: root.job_id}
    else:
        expansion.node_jobs[root_node.name] = root.job_id

    # Create loop sentinel jobs first — non-loop nodes may depend on them.
    for loop in spec.loops:
        sentinel = await _create_loop_sentinel(db, loop, root.job_id, cwd=cwd)
        expansion.sentinel_jobs[loop.name] = sentinel

    # Create non-loop DAG jobs in dependency order so a node's parents are
    # always materialized before the node itself.
    for node in _topo_sort_non_loop(non_loop_nodes):
        if node.name == root_node.name:
            continue
        meta = _node_meta(node, spec.identities, cwd, local_daemon_id)
        parents = [
            _resolve_parent(expansion, node_by_name, node, dep)
            for dep in node.depends_on
        ]
        job = await create_job(
            db,
            provider=meta["provider_hint"] or "claude",
            prompt="",
            identity_did=meta["identity_did"],
            with_step=False,
            work_dir=cwd,
            job_type="sop",
            parents=parents,
            root_job_id=root.job_id,
            sop_node=json.dumps(meta, ensure_ascii=False),
        )
        expansion.node_jobs[node.name] = job.job_id

    # Create iteration 1 jobs for loop nodes. Split into two passes so
    # same-loop parent edges can be wired only after every iteration-1 job
    # id exists — loop-internal cycles make a single dependency-ordered
    # pass impossible.
    for node in loop_nodes:
        if node.name == root_node.name:
            continue
        loop = loop_by_name[node.loop_name]
        meta = _node_meta(node, spec.identities, cwd, local_daemon_id)
        meta["loop_name"] = loop.name
        meta["iteration"] = 1
        external_parents = [
            _resolve_parent(expansion, node_by_name, node, dep)
            for dep in node.depends_on
            if node_by_name[dep].loop_name != node.loop_name
        ]
        job = await create_job(
            db,
            provider=meta["provider_hint"] or "claude",
            prompt="",
            identity_did=meta["identity_did"],
            with_step=False,
            work_dir=cwd,
            job_type="sop",
            parents=external_parents,
            root_job_id=root.job_id,
            sop_node=json.dumps(meta, ensure_ascii=False),
            sop_loop_name=loop.name,
            sop_iteration=1,
            sop_loop_meta=_loop_meta_json(loop),
        )
        expansion.loop_node_jobs.setdefault(node.name, {})[1] = job.job_id

    # Wire same-loop internal edges now that every iteration-1 job exists.
    now = _now()
    for node in loop_nodes:
        this_job = expansion.loop_node_jobs.get(node.name, {}).get(1)
        if this_job is None:  # pragma: no cover — internal invariant
            raise ValueError({
                "type": "sop_internal_job_missing",
                "message": f"loop node '{node.name}' has no iteration-1 job",
                "task": node.name,
            })
        for dep in node.depends_on:
            dep_node = node_by_name[dep]
            if dep_node.loop_name != node.loop_name:
                continue
            parent = expansion.loop_node_jobs.get(dep_node.name, {}).get(1)
            if parent is None:  # pragma: no cover — internal invariant
                raise ValueError({
                    "type": "sop_internal_parent_missing",
                    "message": f"loop task '{node.name}' depends on unresolved task '{dep}'",
                    "task": node.name,
                    "dep": dep,
                })
            db.add(JobParentModel(
                job_id=this_job,
                parent_job_id=parent,
                created_at=now,
            ))

    await db.flush()

    # Activate entry nodes: non-loop no-dep nodes and loop entry nodes with
    # no external (cross-loop / non-loop) parents.
    for node in non_loop_nodes:
        if not node.depends_on:
            await activate_job(db, await _by_id(db, expansion.node_jobs[node.name]))
    for loop in spec.loops:
        entry_node = node_by_name[loop.entry]
        has_external_deps = any(
            node_by_name[d].loop_name != loop.name for d in entry_node.depends_on
        )
        if not has_external_deps:
            await activate_job(db, await _by_id(db, expansion.loop_node_jobs[entry_node.name][1]))

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

    Loop-aware resolution: references to a node inside the same loop use
    the current iteration (or the most recent terminal iteration for
    entry nodes that depend on the previous iteration); references to
    nodes outside the loop use the latest terminal iteration.
    """
    from ...a2a import make_artifact_uri

    text: str = meta.get("input") or ""
    replacements: dict[str, str] = {}
    uris: list[str] = []
    current_loop = job.sop_loop_name
    current_iteration = job.sop_iteration

    for ph in _PLACEHOLDER_RE.findall(text):
        if ph in replacements:
            continue
        node_name, _, output_name = ph.partition(".")
        candidates = [
            up for up in upstreams
            if _node_name_from(up) == node_name
        ]
        if not candidates:
            raise LookupError(
                f"upstream '{node_name}' produced no artifact named '{output_name}'"
            )
        chosen = _preferred_upstream(candidates, current_loop, current_iteration)
        matched: list[ArtifactModel] = []
        for up in chosen:
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


def _node_name_from(job: JobModel) -> str | None:
    meta = json.loads(job.sop_node) if job.sop_node else {}
    return meta.get("name")


def _preferred_upstream(
    candidates: list[JobModel], current_loop: str | None, current_iteration: int
) -> list[JobModel]:
    """Pick the right iteration of an upstream node for placeholder resolution."""
    if current_loop:
        same_loop = [c for c in candidates if c.sop_loop_name == current_loop]
        if same_loop:
            same_iter = [c for c in same_loop if c.sop_iteration == current_iteration]
            if same_iter:
                return same_iter
            terminal = [c for c in same_loop if c.status in _TERMINAL_STATUSES]
            if terminal:
                max_iter = max(c.sop_iteration for c in terminal)
                return [c for c in terminal if c.sop_iteration == max_iter]
            max_iter = max(c.sop_iteration for c in same_loop)
            return [c for c in same_loop if c.sop_iteration == max_iter]

    terminal = [c for c in candidates if c.status in _TERMINAL_STATUSES]
    if terminal:
        max_iter = max(c.sop_iteration for c in terminal)
        return [c for c in terminal if c.sop_iteration == max_iter]
    max_iter = max(c.sop_iteration for c in candidates)
    return [c for c in candidates if c.sop_iteration == max_iter]


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
        job.cwd = explicit_dir
    elif meta.get("local_daemon_id") is not None:
        if target and target == meta.get("local_daemon_id"):
            job.agent_cwd = meta.get("cwd")
            job.cwd = meta.get("cwd")
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
