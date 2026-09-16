/**
 * Pure graph helpers over a {@link SopDoc}: layering, cycle detection,
 * reference indexing and the structural checks the canvas needs.
 *
 * **Scope discipline**: everything here is what a client must know to
 * *draw and edit* a DAG (a node that doesn't resolve cannot be laid out).
 * Business rules stay in Python — ``agt/sop/dsl.py`` is the authority and
 * the editor surfaces its verdict separately (``POST /api/sop/validate``).
 * The rules mirrored here were copied deliberately, so a drift between
 * the two shows up as a server-side error the user can see, never as a
 * silently accepted document.
 */

import type { SopDoc, SopIssue, SopNode } from './model';

/** Mirrors ``agt.sop.dsl.SOP_MAX_NODES``. */
export const SOP_MAX_NODES = 64;

const PLACEHOLDER_RE = /\$\{([A-Za-z0-9_.-]+)\}/g;

/** All ``${node.output}`` references inside a node input. */
export function placeholders(input: string): string[] {
  const out: string[] = [];
  for (const match of input.matchAll(PLACEHOLDER_RE)) {
    out.push(match[1]);
  }
  return out;
}

export function nodesByName(doc: SopDoc): Map<string, SopNode> {
  const map = new Map<string, SopNode>();
  for (const node of doc.nodes) {
    if (node.name !== '' && !map.has(node.name)) {
      map.set(node.name, node);
    }
  }
  return map;
}

/** ``output name → owning node name`` (first declaration wins). */
export function outputOwners(doc: SopDoc): Map<string, string> {
  const owners = new Map<string, string>();
  for (const node of doc.nodes) {
    if (node.output !== '' && !owners.has(node.output)) {
      owners.set(node.output, node.name);
    }
  }
  return owners;
}

/**
 * Nodes that take part in a dependency cycle *or* sit downstream of one
 * (Kahn's algorithm leaves exactly those unvisited).
 */
export function cycleNodes(doc: SopDoc): Set<string> {
  const byName = nodesByName(doc);
  const indegree = new Map<string, number>();
  const dependents = new Map<string, string[]>();
  for (const node of doc.nodes) {
    indegree.set(node.name, 0);
    dependents.set(node.name, []);
  }
  for (const node of doc.nodes) {
    for (const dep of node.dependsOn) {
      if (!byName.has(dep) || dep === node.name) {
        continue;
      }
      indegree.set(node.name, (indegree.get(node.name) ?? 0) + 1);
      dependents.get(dep)?.push(node.name);
    }
  }
  const ready = [...indegree.entries()]
    .filter(([, count]) => count === 0)
    .map(([name]) => name);
  const visited = new Set<string>();
  while (ready.length > 0) {
    const current = ready.pop() as string;
    visited.add(current);
    for (const dependent of dependents.get(current) ?? []) {
      const next = (indegree.get(dependent) ?? 0) - 1;
      indegree.set(dependent, next);
      if (next === 0) {
        ready.push(dependent);
      }
    }
  }
  return new Set(doc.nodes.map((n) => n.name).filter((n) => !visited.has(n)));
}

/**
 * Longest-path layering: a node's layer is ``1 + max(layer of its deps)``.
 *
 * Nodes caught in (or shadowed by) a cycle get their provisional depth so
 * the canvas can still place them, flagged by {@link cycleNodes}.
 */
export function layers(doc: SopDoc): Map<string, number> {
  const byName = nodesByName(doc);
  const memo = new Map<string, number>();
  const visiting = new Set<string>();
  const depth = (name: string): number => {
    const cached = memo.get(name);
    if (cached !== undefined) {
      return cached;
    }
    if (visiting.has(name)) {
      return 0; // cycle: break the recursion, callers flag it separately
    }
    visiting.add(name);
    const node = byName.get(name);
    let layer = 0;
    if (node) {
      for (const dep of node.dependsOn) {
        if (byName.has(dep) && dep !== name) {
          layer = Math.max(layer, depth(dep) + 1);
        }
      }
    }
    visiting.delete(name);
    memo.set(name, layer);
    return layer;
  };
  const result = new Map<string, number>();
  for (const node of doc.nodes) {
    result.set(node.name, depth(node.name));
  }
  return result;
}

export interface ReferenceHit {
  node: string;
  ref: string;
}

/**
 * What a node name change/removal touches: direct dependents and every
 * placeholder reference. Shared by the YAML writer and the impact dialog
 * so "what will change" is computed exactly once.
 */
export function referencesTo(
  doc: SopDoc,
  target: string,
): { dependents: string[]; placeholders: ReferenceHit[] } {
  const dependents: string[] = [];
  const hits: ReferenceHit[] = [];
  for (const node of doc.nodes) {
    if (node.name === target) {
      continue;
    }
    if (node.dependsOn.includes(target)) {
      dependents.push(node.name);
    }
    for (const ref of placeholders(node.input)) {
      if (ref.split('.')[0] === target) {
        hits.push({ node: node.name, ref });
      }
    }
  }
  return { dependents, placeholders: hits };
}

/** A free node name derived from ``base`` (``task``, ``task-2``, …). */
export function uniqueNodeName(doc: SopDoc, base = 'task'): string {
  const used = new Set(doc.nodes.map((n) => n.name));
  if (!used.has(base)) {
    return base;
  }
  let i = 2;
  while (used.has(`${base}-${i}`)) {
    i += 1;
  }
  return `${base}-${i}`;
}

/**
 * Structural issues the canvas must know about. Ordered document-first so
 * the problems panel reads top-down.
 */
export function issues(doc: SopDoc): SopIssue[] {
  const out: SopIssue[] = [];
  if (doc.name.trim() === '') {
    out.push({ level: 'error', message: 'SOP requires a non-empty `name`' });
  }
  if (doc.nodes.length === 0) {
    out.push({ level: 'error', message: '`tasks` must be a non-empty list' });
  }
  if (doc.nodes.length > SOP_MAX_NODES) {
    out.push({
      level: 'error',
      message: `SOP exceeds the node budget: ${doc.nodes.length} > ${SOP_MAX_NODES}`,
    });
  }

  const byName = nodesByName(doc);
  const seen = new Set<string>();
  for (const node of doc.nodes) {
    if (node.name === '') {
      out.push({ level: 'error', message: 'each task requires a non-empty `name`' });
      continue;
    }
    if (seen.has(node.name)) {
      out.push({
        level: 'error',
        node: node.name,
        message: `duplicate task name: ${node.name}`,
      });
    }
    seen.add(node.name);
  }

  const identityAliases = new Set(doc.identities.map((i) => i.alias));
  for (const node of doc.nodes) {
    if (node.name === '') {
      continue;
    }
    if (node.identity !== '' && !identityAliases.has(node.identity)) {
      out.push({
        level: 'error',
        node: node.name,
        field: 'identity',
        message: `task '${node.name}' references unknown identity '${node.identity}'`,
      });
    }
    for (const dep of node.dependsOn) {
      if (dep === node.name) {
        out.push({
          level: 'error',
          node: node.name,
          message: `task '${node.name}' depends on itself`,
        });
      } else if (!byName.has(dep)) {
        out.push({
          level: 'error',
          node: node.name,
          message: `task '${node.name}' depends on unknown task '${dep}'`,
        });
      }
    }
    for (const ref of placeholders(node.input)) {
      const [refNode, outputName] = splitRef(ref);
      if (refNode === '' || outputName === '') {
        out.push({
          level: 'error',
          node: node.name,
          field: 'input',
          message: `task '${node.name}': malformed placeholder '\${${ref}}'`,
        });
        continue;
      }
      const target = byName.get(refNode);
      if (!target) {
        out.push({
          level: 'error',
          node: node.name,
          field: 'input',
          message: `task '${node.name}' references unknown node '\${${refNode}}'`,
        });
        continue;
      }
      if (target.output !== outputName) {
        out.push({
          level: 'error',
          node: node.name,
          field: 'input',
          message: `task '${node.name}' references undeclared output '\${${ref}}'`,
        });
        continue;
      }
      if (!node.dependsOn.includes(refNode)) {
        out.push({
          level: 'error',
          node: node.name,
          field: 'input',
          message:
            `task '${node.name}' references '\${${refNode}}' but does not ` +
            `declare it in depends_on`,
        });
      }
    }
  }

  const outputs = doc.nodes.map((n) => n.output).filter((o) => o !== '');
  for (const dup of new Set(outputs.filter((o, i) => outputs.indexOf(o) !== i))) {
    out.push({ level: 'error', message: `duplicate output names: ['${dup}']` });
  }

  const dependentsOf = new Map<string, number>(doc.nodes.map((n) => [n.name, 0]));
  for (const node of doc.nodes) {
    for (const dep of node.dependsOn) {
      if (dependentsOf.has(dep)) {
        dependentsOf.set(dep, (dependentsOf.get(dep) ?? 0) + 1);
      }
    }
  }
  for (const node of doc.nodes) {
    if (
      node.name !== '' &&
      node.dependsOn.length === 0 &&
      (dependentsOf.get(node.name) ?? 0) === 0
    ) {
      out.push({
        // A DAG root with downstream nodes is perfectly normal and is NOT
        // reported here. Only a node with neither upstream nor downstream is
        // called out — and as info, since a single-node SOP is legal.
        level: 'info',
        node: node.name,
        message:
          `node '${node.name}' has no upstream and no downstream ` +
          `(isolated node — expected for a single-node SOP)`,
      });
    }
  }

  for (const node of cycleNodes(doc)) {
    out.push({
      level: 'error',
      node,
      message: `SOP contains a dependency cycle involving '${node}'`,
    });
  }

  for (const identity of doc.identities) {
    if (identity.did === '' && identity.provider === '') {
      out.push({
        level: 'error',
        message: `identity '${identity.alias}' needs 'provider' and/or 'did'`,
      });
    }
  }

  return out;
}

export function splitRef(ref: string): [string, string] {
  const dot = ref.indexOf('.');
  if (dot < 0) {
    return [ref, ''];
  }
  return [ref.slice(0, dot), ref.slice(dot + 1)];
}

/** Rewrite every ``${from.output}`` placeholder in ``input`` to ``to``. */
export function rewritePlaceholders(
  input: string,
  from: string,
  to: string,
): string {
  return input.replace(
    new RegExp(`\\$\\{${escapeRegExp(from)}\\.([A-Za-z0-9_.-]+)\\}`, 'g'),
    (_match, outputName: string) => `\${${to}.${outputName}}`,
  );
}

export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** Would adding ``to → from`` create a cycle? */
export function wouldCycle(
  doc: SopDoc,
  node: string,
  dependsOn: string,
): boolean {
  if (node === dependsOn) {
    return true;
  }
  const byName = nodesByName(doc);
  const stack = [dependsOn];
  const seen = new Set<string>();
  while (stack.length > 0) {
    const current = stack.pop() as string;
    if (current === node) {
      return true;
    }
    if (seen.has(current)) {
      continue;
    }
    seen.add(current);
    for (const dep of byName.get(current)?.dependsOn ?? []) {
      stack.push(dep);
    }
  }
  return false;
}
