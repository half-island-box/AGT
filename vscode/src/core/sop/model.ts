/**
 * SOP document model — the shape the editor host and the webview share.
 *
 * Field-for-field mirror of ``agt/sop/dsl.py``, but **without any
 * validation rules**: parse/validation authority stays in Python (the
 * editor calls ``POST /api/sop/validate``). This model is what the canvas
 * draws and what edit intents are expressed against.
 *
 * Optional DSL fields are normalized to ``''`` (absent) so the UI binds
 * to a single representation; the YAML writer drops empty optional keys.
 */

export interface SopIdentity {
  alias: string;
  did: string;
  provider: string;
}

export interface SopNode {
  name: string;
  identity: string;
  dependsOn: string[];
  input: string;
  output: string;
  provider: string;
  daemon: string;
  dir: string;
  /** Position in the YAML ``tasks`` sequence — the authoritative order. */
  index: number;
}

export interface SopLoop {
  name: string;
  nodes: string[];
  maxIterations: number;
  entry: string;
}

export interface SopDoc {
  name: string;
  identities: SopIdentity[];
  nodes: SopNode[];
  loops: SopLoop[];
}

export type SopLoopField =
  | 'name'
  | 'maxIterations'
  | 'entry';

/** Node fields the inspector can edit (``name`` has its own intent). */
export type SopNodeField =
  | 'input'
  | 'output'
  | 'identity'
  | 'provider'
  | 'daemon'
  | 'dir';

export const SOP_OPTIONAL_FIELDS: readonly SopNodeField[] = [
  'output',
  'identity',
  'provider',
  'daemon',
  'dir',
];

export type EditIntent =
  | { kind: 'setName'; name: string }
  | { kind: 'addNode'; after?: string | null; name?: string }
  | { kind: 'deleteNode'; name: string }
  | { kind: 'renameNode'; from: string; to: string }
  | { kind: 'setNodeField'; name: string; field: SopNodeField; value: string }
  | { kind: 'addDependency'; node: string; dependsOn: string }
  | { kind: 'removeDependency'; node: string; dependsOn: string }
  | { kind: 'addIdentity'; alias: string; did?: string; provider?: string }
  | { kind: 'removeIdentity'; alias: string }
  | {
      kind: 'setIdentityField';
      alias: string;
      field: 'did' | 'provider';
      value: string;
    }
  | { kind: 'addLoop'; name?: string; nodes?: string[] }
  | { kind: 'removeLoop'; name: string }
  | {
      kind: 'setLoopField';
      name: string;
      field: SopLoopField;
      value: string | number;
    }
  | { kind: 'setLoopNodes'; name: string; nodes: string[] };

/**
 * ``error`` blocks a run, ``warning`` is likely a mistake, ``info`` is
 * informational — e.g. an isolated node, which is perfectly legal for a
 * single-node SOP but worth pointing out.
 */
export type SopIssueLevel = 'error' | 'warning' | 'info';

export interface SopIssue {
  level: SopIssueLevel;
  /** Node name the issue belongs to (absent for document-level issues). */
  node?: string;
  /** Node field the issue belongs to, when it is field-specific. */
  field?: string;
  message: string;
}

export function emptyDoc(): SopDoc {
  return { name: '', identities: [], nodes: [], loops: [] };
}

function asString(value: unknown): string {
  if (typeof value === 'string') {
    return value;
  }
  if (value === null || value === undefined) {
    return '';
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return '';
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) {
    return [];
  }
  return value.map(asString).filter((item) => item !== '');
}

function asLoopIterations(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) ? Math.trunc(value) : 0;
}

/**
 * Normalize a plain object (from the YAML document or a test fixture)
 * into a {@link SopDoc}. Unknown keys are ignored here — they survive in
 * the YAML document itself, which is what round-trip fidelity relies on.
 */
export function fromPlainObject(value: unknown): SopDoc {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return emptyDoc();
  }
  const raw = value as Record<string, unknown>;
  const identitiesRaw = raw.identities;
  const identities: SopIdentity[] = [];
  if (
    typeof identitiesRaw === 'object' &&
    identitiesRaw !== null &&
    !Array.isArray(identitiesRaw)
  ) {
    for (const [alias, entry] of Object.entries(
      identitiesRaw as Record<string, unknown>,
    )) {
      if (typeof entry === 'string') {
        identities.push({ alias, did: '', provider: asString(entry) });
        continue;
      }
      if (typeof entry === 'object' && entry !== null) {
        const e = entry as Record<string, unknown>;
        identities.push({
          alias,
          did: asString(e.did),
          provider: asString(e.provider),
        });
      } else {
        identities.push({ alias, did: '', provider: '' });
      }
    }
  }

  const nodes: SopNode[] = [];
  const tasks = Array.isArray(raw.tasks) ? raw.tasks : [];
  tasks.forEach((entry, index) => {
    if (typeof entry !== 'object' || entry === null) {
      return;
    }
    const t = entry as Record<string, unknown>;
    nodes.push({
      name: asString(t.name),
      identity: asString(t.identity),
      dependsOn: asStringList(t.depends_on),
      input: asString(t.input),
      output: asString(t.output),
      provider: asString(t.provider),
      daemon: asString(t.daemon),
      dir: asString(t.dir),
      index,
    });
  });

  const loops: SopLoop[] = [];
  const loopsRaw = raw.loops;
  if (Array.isArray(loopsRaw)) {
    for (const entry of loopsRaw) {
      if (typeof entry !== 'object' || entry === null) {
        continue;
      }
      const loop = entry as Record<string, unknown>;
      loops.push({
        name: asString(loop.name),
        nodes: asStringList(loop.nodes),
        maxIterations: asLoopIterations(loop.max_iterations),
        entry: asString(loop.entry),
      });
    }
  }

  return { name: asString(raw.name), identities, nodes, loops };
}
