"""SOP DSL — parse + statically validate a YAML/JSON DAG (Phase 5).

A SOP is a human-authored static pipeline::

    name: review-fix
    identities:
      reviewer: {did: did:agt:reviewer, provider: claude}
      tester:   {provider: codex}          # alias → provider constraint
    tasks:
      - name: review
        identity: reviewer
        input: 审查当前 diff，输出审查报告
        output: review-report
      - name: test
        identity: tester
        input: 用 ${review.review-report} 指导写单测
        depends_on: [review]
        output: test-result

Validation (all fatal): DAG non-empty, ≤64 nodes, acyclic, depends_on /
identity references exist, output names unique. Orphan nodes warn only.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

import yaml

log = logging.getLogger(__name__)

SOP_MAX_NODES = 64

_PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")


class SOPError(ValueError):
    """Static validation failure with a machine-readable message."""


@dataclass
class SOPIdentity:
    """An ``identities`` entry: alias → routing signal.

    ``did`` (optional) points at a real server identity — routing then
    resolves the provider from the identity's bindings (validated to
    exist + be active at submit). Without ``did`` the alias's ``provider``
    is a hard provider constraint: submit checks a live runtime offers it.
    """

    name: str
    provider: str | None = None
    did: str | None = None


@dataclass
class SOPNode:
    name: str
    identity: str | None = None
    depends_on: list[str] = field(default_factory=list)
    input: str = ""
    output: str | None = None
    provider: str | None = None
    daemon: str | None = None
    dir: str | None = None

    def placeholders(self) -> list[str]:
        return _PLACEHOLDER_RE.findall(self.input)


@dataclass
class SOPEdge:
    dependent: str
    upstream: str


@dataclass
class SOPSpec:
    name: str
    nodes: list[SOPNode]
    edges: list[SOPEdge]
    identities: dict[str, SOPIdentity]

    def node(self, name: str) -> SOPNode:
        for n in self.nodes:
            if n.name == name:
                return n
        raise SOPError(f"unknown node: {name}")


def parse_sop(raw: str | dict) -> SOPSpec:
    """Parse + validate a SOP document (YAML or JSON dict/str).

    Raises :class:`SOPError` on any validation failure.
    """
    data = raw if isinstance(raw, dict) else yaml.safe_load(raw)
    if not isinstance(data, dict):
        raise SOPError("SOP document must be a mapping")

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise SOPError("SOP requires a non-empty 'name'")

    identities = _parse_identities(data.get("identities"))
    nodes = _parse_tasks(data.get("tasks"), identities)
    _validate_graph(nodes)
    edges = [
        SOPEdge(dependent=n.name, upstream=dep) for n in nodes for dep in n.depends_on
    ]
    _validate_placeholders(nodes)
    return SOPSpec(name=name, nodes=nodes, edges=edges, identities=identities)


def _parse_identities(raw) -> dict[str, SOPIdentity]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SOPError("'identities' must be a mapping")
    result: dict[str, SOPIdentity] = {}
    for alias, entry in raw.items():
        if not isinstance(alias, str) or not alias:
            raise SOPError("identity alias must be a non-empty string")
        if isinstance(entry, str):
            entry = {"provider": entry}
        if not isinstance(entry, dict):
            raise SOPError(f"identity '{alias}' must be a mapping or provider string")
        did = entry.get("did")
        provider = entry.get("provider")
        if did is not None and not isinstance(did, str):
            raise SOPError(f"identity '{alias}': 'did' must be a string")
        if provider is not None and not isinstance(provider, str):
            raise SOPError(f"identity '{alias}': 'provider' must be a string")
        if did is None and provider is None:
            raise SOPError(f"identity '{alias}' needs 'provider' and/or 'did'")
        result[alias] = SOPIdentity(name=alias, provider=provider, did=did)
    return result


def _parse_tasks(raw, identities: dict[str, SOPIdentity]) -> list[SOPNode]:
    if not isinstance(raw, list) or not raw:
        raise SOPError("'tasks' must be a non-empty list")
    if len(raw) > SOP_MAX_NODES:
        raise SOPError(f"SOP exceeds the node budget: {len(raw)} > {SOP_MAX_NODES}")

    nodes: list[SOPNode] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise SOPError("each task must be a mapping")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise SOPError("each task requires a non-empty 'name'")
        if name in seen:
            raise SOPError(f"duplicate task name: {name}")
        seen.add(name)

        identity = entry.get("identity")
        if identity is not None:
            if not isinstance(identity, str):
                raise SOPError(f"task '{name}': 'identity' must be a string")
            if identity not in identities:
                raise SOPError(f"task '{name}' references unknown identity '{identity}'")

        depends_on = entry.get("depends_on") or []
        if not isinstance(depends_on, list) or not all(
            isinstance(d, str) for d in depends_on
        ):
            raise SOPError(f"task '{name}': 'depends_on' must be a list of names")
        if name in depends_on:
            raise SOPError(f"task '{name}' depends on itself")

        output = entry.get("output")
        if output is not None and not isinstance(output, str):
            raise SOPError(f"task '{name}': 'output' must be a string")
        inp = entry.get("input")
        if inp is not None and not isinstance(inp, str):
            raise SOPError(f"task '{name}': 'input' must be a string")
        provider = entry.get("provider")
        daemon = entry.get("daemon")
        dir_ = entry.get("dir")
        if provider is not None and not isinstance(provider, str):
            raise SOPError(f"task '{name}': 'provider' must be a string")
        if daemon is not None and not isinstance(daemon, str):
            raise SOPError(f"task '{name}': 'daemon' must be a string")
        if dir_ is not None and not isinstance(dir_, str):
            raise SOPError(f"task '{name}': 'dir' must be a string")

        nodes.append(
            SOPNode(
                name=name,
                identity=identity,
                depends_on=list(depends_on),
                input=inp or "",
                output=output,
                provider=provider,
                daemon=daemon,
                dir=dir_,
            )
        )

    outputs = [n.output for n in nodes if n.output]
    dupes = {o for o in outputs if outputs.count(o) > 1}
    if dupes:
        raise SOPError(f"duplicate output names: {sorted(dupes)}")
    return nodes


def _validate_graph(nodes: list[SOPNode]) -> None:
    by_name = {n.name: n for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            if dep not in by_name:
                raise SOPError(f"task '{n.name}' depends on unknown task '{dep}'")

    # Cycle detection: Kahn's algorithm; orphans are a warning only.
    indeg = {n.name: len(n.depends_on) for n in nodes}
    ready = [n.name for n in nodes if indeg[n.name] == 0]
    dependents: dict[str, list[str]] = {n.name: [] for n in nodes}
    for n in nodes:
        for dep in n.depends_on:
            dependents[dep].append(n.name)
    visited = 0
    while ready:
        current = ready.pop()
        visited += 1
        for d in dependents[current]:
            indeg[d] -= 1
            if indeg[d] == 0:
                ready.append(d)
    if visited != len(nodes):
        raise SOPError("SOP contains a dependency cycle")

    # Orphan warning: a node with no incoming AND no outgoing edges.
    for n in nodes:
        if not n.depends_on and not dependents[n.name]:
            log.warning("SOP node '%s' is an orphan (no deps, no dependents)", n.name)


def _validate_placeholders(nodes: list[SOPNode]) -> None:
    """``${node.output}`` must reference a known node and declared output."""
    by_name = {n.name: n for n in nodes}
    for n in nodes:
        for ph in n.placeholders():
            node_name, sep, output_name = ph.partition(".")
            if not sep or not node_name or not output_name:
                raise SOPError(f"task '{n.name}': malformed placeholder '${{{ph}}}'")
            target = by_name.get(node_name)
            if target is None:
                raise SOPError(
                    f"task '{n.name}' references unknown node '${{{node_name}}}'"
                )
            if target.output != output_name:
                raise SOPError(
                    f"task '{n.name}' references undeclared output "
                    f"'${{{node_name}.{output_name}}}'"
                )
            if node_name not in n.depends_on:
                raise SOPError(
                    f"task '{n.name}' references '${{{node_name}}}' but does not "
                    f"declare it in depends_on"
                )


__all__ = [
    "SOP_MAX_NODES",
    "SOPError",
    "SOPIdentity",
    "SOPNode",
    "SOPEdge",
    "SOPSpec",
    "parse_sop",
]
