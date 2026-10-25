"""Goal→SOP export (goal mode G5).

Pure-read transform of a persisted goal run (Job tree + Step
``input_message`` + artifact registry) into a static SOP YAML document.
The transform mirrors the Step **event** graph, not the Job tree:

- leaf Jobs (never spawned) → one task;
- spawned Jobs → two tasks: ``<n>`` (collapse of all non-final Steps)
  and ``<n>-synth`` (the final Step).

Output is a *draft* for human review: it re-expresses data flow as
``${producer.output}`` placeholders and ``depends_on`` edges, resolves
all representable conflicts first, and only then emits — so a generated
document either passes ``parse_sop`` or the export fails, never returns
an illegal YAML as if it were valid.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...sop.dsl import SOPError, parse_sop
from ..models import ArtifactModel, JobModel

_SLUG_RE = re.compile(r"[^\w-]+", re.UNICODE)


@dataclass
class _Vertex:
    name: str
    job: JobModel
    kind: str  # "leaf" | "plan" | "synth"
    step_ids: list[str] = field(default_factory=list)
    artifact_ids: list[str] = field(default_factory=list)
    # ("text", text) | ("file", artifact_id)
    parts: list[tuple[str, str]] = field(default_factory=list)
    identity_alias: str | None = None
    depends_on: list[str] = field(default_factory=list)
    dropped: bool = False


def _slug(text: str, fallback: str = "task") -> str:
    """Goal text → safe task name: no dots / ``${}``, whitespace → ``-``."""
    collapsed = " ".join((text or "").split())
    slug = _SLUG_RE.sub("-", collapsed).strip("-_")
    if not slug:
        slug = fallback
    return slug[:32] or fallback


def _parse_goal_meta(root: JobModel) -> dict | None:
    if not root.goal_meta:
        return None
    try:
        meta = json.loads(root.goal_meta)
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) and meta.get("mode") == "goal" else None


def _ordered_steps(job: JobModel) -> list:
    steps = list(job.steps or [])
    steps.sort(key=lambda s: (s.created_at or "", s.step_id or ""))
    return steps


def _step_parts(step) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse a Step's ``input_message`` into export parts + FilePart ids.

    TextPart → verbatim; FilePart → a ``("file", artifact_id)`` marker;
    DataPart → JSON text unless it is orchestration metadata (the synthesis
    children-status block carries an ``children`` key and is re-expressed
    as structural edges instead).
    """
    parts: list[tuple[str, str]] = []
    artifact_ids: list[str] = []
    if not step.input_message:
        return parts, artifact_ids
    try:
        message = json.loads(step.input_message)
    except (TypeError, ValueError):
        return parts, artifact_ids
    if not isinstance(message, dict):
        return parts, artifact_ids
    prefix = "agt-artifact://"
    for part in message.get("parts") or []:
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype == "text":
            text = part.get("text")
            if isinstance(text, str) and text:
                parts.append(("text", text))
        elif ptype == "file":
            uri = part.get("uri")
            if isinstance(uri, str):
                aid = uri[len(prefix):] if uri.startswith(prefix) else uri
                artifact_ids.append(aid)
                parts.append(("file", aid))
        elif ptype == "data":
            data = part.get("data")
            if isinstance(data, dict) and "children" in data:
                continue  # synthesis status metadata → structural edges
            if data is not None:
                parts.append(
                    ("text", "Structured input (JSON):\n"
                     + json.dumps(data, ensure_ascii=False, indent=2))
                )
    return parts, artifact_ids


def _first_text(job: JobModel) -> str:
    steps = _ordered_steps(job)
    if not steps:
        return ""
    parts, _ = _step_parts(steps[0])
    texts = [t for k, t in parts if k == "text"]
    return texts[0] if texts else (steps[0].prompt or "")


def _fill_vertex(vertex: _Vertex, steps: list, needs_input_notes: bool = False) -> None:
    """Populate a vertex's input parts from its Steps.

    First Step's input is the task input; a later Step that *answers* a
    ``needs_input`` (previous Step's outcome) is appended as a
    ``[用户回答: …]`` note rather than treated as a fresh input.
    """
    if not steps:
        return
    parts, _ = _step_parts(steps[0])
    vertex.parts.extend(parts)
    if needs_input_notes:
        for i in range(1, len(steps)):
            if steps[i - 1].outcome == "needs_input":
                answer = (steps[i].prompt or "").strip()
                if answer:
                    vertex.parts.append(("text", f"[用户回答: {answer}]"))
    if not vertex.parts:
        vertex.parts.append(("text", steps[0].prompt or ""))


async def export_goal(
    db: AsyncSession,
    root_job_id: str,
    *,
    include_failed: bool = False,
    keep_planning: bool = False,
) -> dict:
    """Export a goal run to a SOP YAML draft. Returns ``{yaml, warnings}``.

    Raises ``ValueError`` (``{"type", "message"}``) for a non-goal/unknown
    root, or when the fully-resolved document still fails ``parse_sop``
    (internal error — never an illegal emit).
    """
    root = await db.get(JobModel, root_job_id)
    if root is None:
        raise ValueError({"type": "job_not_found", "message": root_job_id})
    meta = _parse_goal_meta(root)
    if meta is None:
        raise ValueError({
            "type": "not_goal_root",
            "message": f"job {root_job_id} is not a goal run",
        })

    warnings: list[str] = []

    rows = (
        await db.execute(
            select(JobModel).where(JobModel.root_job_id == root_job_id)
        )
    ).scalars().all()
    job_by_id = {j.job_id: j for j in rows}
    job_by_id.setdefault(root.job_id, root)

    child_ids: dict[str, list[str]] = {}
    for j in list(job_by_id.values()):
        if j.parent_job_id and j.parent_job_id in job_by_id:
            child_ids.setdefault(j.parent_job_id, []).append(j.job_id)

    artifacts = (
        await db.execute(
            select(ArtifactModel).where(
                ArtifactModel.job_id.in_(list(job_by_id.keys()))
            )
        )
    ).scalars().all()
    artifact_name: dict[str, str] = {}
    artifact_step: dict[str, str] = {}
    for a in artifacts:
        artifact_name[a.artifact_id] = a.name
        artifact_step[a.artifact_id] = a.step_id

    # Failed-wave exclusion: a failed Job and its whole subtree (the wave a
    # synthesis retry abandons) are dropped unless include_failed.
    excluded: set[str] = set()
    if not include_failed:
        for j in list(job_by_id.values()):
            if j.job_id in excluded or j.status != "failed":
                continue
            frontier = [j.job_id]
            wave = [j.job_id]
            while frontier:
                found = (
                    await db.execute(
                        select(JobModel.job_id).where(
                            JobModel.parent_job_id.in_(frontier)
                        )
                    )
                ).scalars().all()
                frontier = [r for r in found if r not in wave]
                wave.extend(frontier)
            excluded.update(wave)
            warnings.append(
                f"排除失败波子任务 {', '.join(wave)}（include_failed=false）"
            )

    kept_jobs = [j for j in job_by_id.values() if j.job_id not in excluded]

    # Unique task names: claim each kept job's base slug first, then each
    # spawned job's ``-synth`` suffix, so the full vertex-name set is
    # globally unique from the start (no late rename rewriting needed).
    used_names: set[str] = set()

    def _claim(name: str) -> str:
        candidate = name
        i = 2
        while candidate in used_names:
            candidate = f"{name}-{i}"
            i += 1
        used_names.add(candidate)
        return candidate

    slug_of: dict[str, str] = {}
    for j in kept_jobs:
        goal_text = meta.get("goal") if j.job_id == root.job_id else _first_text(j)
        slug_of[j.job_id] = _claim(_slug(goal_text))

    synth_name: dict[str, str] = {}
    for j in kept_jobs:
        if _spawned(j, child_ids):
            synth_name[j.job_id] = _claim(f"{slug_of[j.job_id]}-synth")

    # Build vertices (Step event graph).
    vertices: list[_Vertex] = []
    vertex_by_step: dict[str, _Vertex] = {}
    for j in kept_jobs:
        steps = _ordered_steps(j)
        spawned = _spawned(j, child_ids)
        base = slug_of[j.job_id]
        if not spawned:
            v = _Vertex(name=base, job=j, kind="leaf",
                        step_ids=[s.step_id for s in steps])
            _fill_vertex(v, steps)
            vertices.append(v)
            continue
        plan_steps = steps[:-1] if len(steps) > 1 else steps
        synth_steps = steps[-1:]
        plan = _Vertex(name=base, job=j, kind="plan",
                       step_ids=[s.step_id for s in plan_steps])
        _fill_vertex(plan, plan_steps, needs_input_notes=True)
        synth = _Vertex(name=synth_name[j.job_id], job=j, kind="synth",
                        step_ids=[s.step_id for s in synth_steps])
        _fill_vertex(synth, synth_steps)
        vertices.extend([plan, synth])

    for v in vertices:
        for sid in v.step_ids:
            vertex_by_step[sid] = v

    # Artifact → owning vertex (via its Step), within a kept job.
    artifact_owner: dict[str, _Vertex] = {}
    for aid, sid in artifact_step.items():
        owner = vertex_by_step.get(sid)
        if owner is not None:
            artifact_owner[aid] = owner
            if aid not in owner.artifact_ids:
                owner.artifact_ids.append(aid)

    # Consumption: which artifact names each producer has consumed by others.
    consumed_names: dict[str, list[str]] = {}
    for v in vertices:
        seen: list[str] = []
        for kind, payload in v.parts:
            if kind == "file" and payload not in seen:
                seen.append(payload)
        for aid in seen:
            producer = artifact_owner.get(aid)
            if producer is None or producer is v:
                continue
            name = artifact_name.get(aid)
            bucket = consumed_names.setdefault(producer.name, [])
            if name and name not in bucket:
                bucket.append(name)

    # Planning-round drop: a plan vertex nobody reads is dropped (its
    # outputs are unconsumed). keep_planning keeps it as an orphan.
    for v in vertices:
        if v.kind != "plan" or consumed_names.get(v.name):
            continue
        if keep_planning:
            warnings.append(f"保留纯规划轮 task '{v.name}'（keep_planning=true）")
        else:
            v.dropped = True
            warnings.append(f"丢弃纯规划轮 task '{v.name}'（产物无人消费）")

    # Output binding: main name wins; further consumed names downgrade.
    output_main: dict[str, str] = {}
    downgraded: dict[str, list[str]] = {}
    for v in vertices:
        names = consumed_names.get(v.name)
        if not names:
            continue
        output_main[v.name] = names[0]
        if len(names) > 1:
            downgraded[v.name] = names[1:]
            for extra in names[1:]:
                warnings.append(
                    f"task '{v.name}' 多个产物被消费，主产物取 '{names[0]}'，"
                    f"'{extra}' 降级为文字附注"
                )

    # Cross-producer same-name conflicts → unique output required, so rename
    # colliding outputs to '<task-name>/<name>' and rewrite references.
    by_output: dict[str, list[str]] = {}
    for vname, out in output_main.items():
        by_output.setdefault(out, []).append(vname)
    final_output: dict[str, str] = {}
    for out, owners in by_output.items():
        for vname in owners:
            if len(owners) > 1:
                final_output[vname] = f"{vname}/{out}"
                warnings.append(
                    f"跨 task 同名产物 '{out}' 已对 task '{vname}' 改名为 "
                    f"'{vname}/{out}'"
                )
            else:
                final_output[vname] = out

    # Structural edges: each synth task waits on its children's representative.
    for v in vertices:
        if v.kind != "synth":
            continue
        for child in child_ids.get(v.job.job_id) or []:
            if child in excluded or child not in job_by_id:
                continue
            child_job = job_by_id[child]
            cspawned = _spawned(child_job, child_ids)
            rep = synth_name.get(child, slug_of.get(child)) if cspawned else slug_of.get(child)
            if rep and rep not in v.depends_on:
                v.depends_on.append(rep)

    # Data edges + input rewrite into ${producer.output} / note.
    for v in vertices:
        if v.dropped:
            continue
        blocks: list[str] = []
        for kind, payload in v.parts:
            if kind == "text":
                blocks.append(payload)
                continue
            aid = payload
            producer = artifact_owner.get(aid)
            name = artifact_name.get(aid, "")
            if producer is None or producer.dropped or producer is v:
                warnings.append(
                    f"task '{v.name}' 引用了不可映射的 artifact '{name or aid}'"
                )
                blocks.append(f"[artifact {name or aid} 未映射，需手工接线]")
                continue
            if name in downgraded.get(producer.name, []):
                warnings.append(
                    f"task '{v.name}' 对 '{producer.name}.{name}' 的引用降级为文字附注"
                )
                blocks.append(f"[artifact {name} of {producer.name} 未映射，需手工接线]")
            elif name != output_main.get(producer.name):
                warnings.append(
                    f"task '{v.name}' 引用了 '{producer.name}' 未声明产物 '{name}'"
                )
                blocks.append(f"[artifact {name} of {producer.name} 未映射，需手工接线]")
            else:
                out = final_output.get(producer.name, name)
                blocks.append(f"${{{producer.name}.{out}}}")
                if producer.name not in v.depends_on:
                    v.depends_on.append(producer.name)
        v.parts = [("text", b) for b in blocks if b]
        v.depends_on = sorted(set(v.depends_on))

    # Identity aggregation: did → did-alias, else provider → provider-alias.
    identities: dict[str, dict] = {}
    for v in vertices:
        if v.dropped:
            continue
        if v.job.identity_did:
            did_slug = _slug(v.job.identity_did.rsplit(":", 1)[-1], "id")
            alias = f"did-{did_slug}"
            existing = [k for k, val in identities.items()
                        if val.get("did") == v.job.identity_did]
            if existing:
                alias = existing[0]
            else:
                i = 2
                while alias in identities:
                    alias = f"did-{did_slug}-{i}"
                    i += 1
                identities[alias] = {"did": v.job.identity_did}
            v.identity_alias = alias
        else:
            alias = v.job.provider or "default"
            identities.setdefault(alias, {"provider": alias})
            v.identity_alias = alias

    tasks: list[dict] = []
    for v in vertices:
        if v.dropped:
            continue
        task = {
            "name": v.name,
            "input": "\n\n".join(t for _, t in v.parts if t) or "",
        }
        if v.identity_alias:
            task["identity"] = v.identity_alias
        if final_output.get(v.name):
            task["output"] = final_output[v.name]
        if v.depends_on:
            task["depends_on"] = v.depends_on
        tasks.append(task)

    root_goal = meta.get("goal") or _first_text(root)
    doc = {
        "name": _slug(root_goal, "goal"),
        "identities": identities,
        "tasks": tasks,
    }

    try:
        parse_sop(doc)
    except SOPError as exc:
        raise ValueError({
            "type": "export_invalid",
            "message": f"export produced an invalid SOP: {exc}",
        }) from None

    yaml_text = yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)
    return {"yaml": yaml_text, "warnings": warnings}


def _spawned(job: JobModel, child_ids: dict[str, list[str]]) -> bool:
    return any(s.outcome == "spawn_subjob" for s in job.steps) or bool(
        child_ids.get(job.job_id)
    )


__all__ = ["export_goal"]
