/**
 * Round-trip layer: YAML text ⇄ `yaml` Document AST ⇄ {@link SopDoc}.
 *
 * **Node-only** (the `yaml` dependency never reaches the webview bundle):
 * the host parses, the host writes. Every mutation goes through the
 * Document AST, so comments, key order, quoting style and unknown keys
 * belonging to untouched parts of the file survive an edit — that is the
 * whole point of not regenerating the document from the model.
 *
 * Scalar updates reuse the existing node (`getIn(path, true)`) instead of
 * replacing it, which is what keeps a trailing `# comment` attached to the
 * value the user just typed in the canvas.
 */

import { Document, YAMLMap, YAMLSeq, isMap, isScalar, isSeq, parseDocument } from 'yaml';
import { rewritePlaceholders, uniqueNodeName } from './graph';
import {
  SOP_OPTIONAL_FIELDS,
  emptyDoc,
  fromPlainObject,
  type EditIntent,
  type SopDoc,
} from './model';

export interface ParsedSop {
  doc: Document;
  model: SopDoc;
  /** Set when the text cannot be parsed as YAML at all. */
  parseError: string | null;
}

export function parseSopText(text: string): ParsedSop {
  const doc = parseDocument(text);
  if (doc.errors.length > 0) {
    return { doc, model: emptyDoc(), parseError: doc.errors[0].message };
  }
  return { doc, model: fromPlainObject(doc.toJS()), parseError: null };
}

/**
 * Print the document.
 *
 * ``lineWidth: 0`` disables folding (long ``input`` prompts must not be
 * re-wrapped) and ``flowCollectionPadding: false`` keeps hand-written
 * ``[a, b]`` / ``{k: v}`` flow style compact instead of respacing it.
 * Remaining caveat: indentation and quote style are the library's, so a
 * file that was hand-formatted with unusual whitespace may see cosmetic
 * reflow on its first save — comments, key order and unknown keys always
 * survive.
 */
export function serializeSop(parsed: ParsedSop): string {
  return parsed.doc.toString({ lineWidth: 0, flowCollectionPadding: false });
}

/** Apply one edit intent in place and re-derive the model. */
export function applyIntent(parsed: ParsedSop, intent: EditIntent): ParsedSop {
  const { doc, model } = parsed;
  if (parsed.parseError !== null) {
    return parsed; // never edit a document we could not parse
  }

  switch (intent.kind) {
    case 'setName':
      setScalar(doc, ['name'], intent.name);
      break;

    case 'addNode': {
      const name = intent.name?.trim() || uniqueNodeName(model);
      const entry: Record<string, unknown> = { name, input: '' };
      if (intent.after) {
        entry.depends_on = [intent.after];
      }
      ensureTasksSeq(doc).add(doc.createNode(entry));
      break;
    }

    case 'deleteNode': {
      const index = taskIndexByName(doc, intent.name);
      if (index < 0) {
        break;
      }
      // Strip inbound references first — dropping the task while other
      // nodes still point at it would leave a document that cannot be
      // validated. Placeholders inside `input` are left alone on purpose:
      // silently rewriting a user's prompt text is worse than an error the
      // problems panel points at.
      for (const item of tasksItems(doc)) {
        const deps = item.get('depends_on');
        if (!isSeq(deps)) {
          continue;
        }
        for (let i = deps.items.length - 1; i >= 0; i -= 1) {
          if (scalarText(deps.items[i]) === intent.name) {
            deps.delete(i);
          }
        }
        if (deps.items.length === 0) {
          item.delete('depends_on');
        }
      }
      tasksSeq(doc)?.delete(index);
      break;
    }

    case 'renameNode': {
      const index = taskIndexByName(doc, intent.from);
      if (index < 0 || intent.from === intent.to) {
        break;
      }
      setScalar(doc, ['tasks', index, 'name'], intent.to);
      for (const item of tasksItems(doc)) {
        const deps = item.get('depends_on');
        if (isSeq(deps)) {
          for (const dep of deps.items) {
            if (isScalar(dep) && dep.value === intent.from) {
              dep.value = intent.to;
            }
          }
        }
        // ``keepScalar`` matters: without it the scalar is unwrapped and the
        // rewritten text would be written back into a fresh string.
        const input = item.get('input', true);
        if (isScalar(input) && typeof input.value === 'string') {
          input.value = rewritePlaceholders(input.value, intent.from, intent.to);
        }
      }
      break;
    }

    case 'setNodeField': {
      const index = taskIndexByName(doc, intent.name);
      if (index < 0) {
        break;
      }
      const path: (string | number)[] = ['tasks', index, intent.field];
      if (intent.value === '' && SOP_OPTIONAL_FIELDS.includes(intent.field)) {
        doc.deleteIn(path);
      } else {
        setScalar(doc, path, intent.value);
      }
      break;
    }

    case 'addDependency': {
      const index = taskIndexByName(doc, intent.node);
      if (index < 0) {
        break;
      }
      const existing = doc.getIn(['tasks', index, 'depends_on']);
      if (isSeq(existing)) {
        if (!existing.items.some((i) => scalarText(i) === intent.dependsOn)) {
          existing.add(intent.dependsOn);
        }
      } else {
        doc.setIn(['tasks', index, 'depends_on'], [intent.dependsOn]);
      }
      break;
    }

    case 'removeDependency': {
      const index = taskIndexByName(doc, intent.node);
      if (index < 0) {
        break;
      }
      const item = taskAt(doc, index);
      const deps = item?.get('depends_on');
      if (!item || !isSeq(deps)) {
        break;
      }
      for (let i = deps.items.length - 1; i >= 0; i -= 1) {
        if (scalarText(deps.items[i]) === intent.dependsOn) {
          deps.delete(i);
        }
      }
      if (deps.items.length === 0) {
        item.delete('depends_on');
      }
      break;
    }

    case 'addIdentity': {
      const entry: Record<string, string> = {};
      if (intent.did) {
        entry.did = intent.did;
      }
      if (intent.provider) {
        entry.provider = intent.provider;
      }
      doc.setIn(['identities', intent.alias], doc.createNode(entry));
      break;
    }

    case 'removeIdentity': {
      doc.deleteIn(['identities', intent.alias]);
      const identities = doc.get('identities');
      if (isMap(identities) && identities.items.length === 0) {
        doc.delete('identities');
      }
      break;
    }

    case 'setIdentityField': {
      const aliasPath: (string | number)[] = ['identities', intent.alias];
      const current = doc.getIn(aliasPath, true);
      if (!isMap(current)) {
        // The DSL also accepts shorthand (`tester: codex`). Editing a field
        // of such an alias rewrites it as a mapping, keeping the provider
        // that the shorthand was carrying.
        const shorthandProvider =
          isScalar(current) && typeof current.value === 'string'
            ? current.value
            : '';
        const entry: Record<string, string> = {};
        const provider =
          intent.field === 'provider' ? intent.value : shorthandProvider;
        if (provider !== '') {
          entry.provider = provider;
        }
        if (intent.field === 'did' && intent.value !== '') {
          entry.did = intent.value;
        }
        if (Object.keys(entry).length === 0) {
          doc.deleteIn(aliasPath);
        } else {
          doc.setIn(aliasPath, doc.createNode(entry));
        }
        break;
      }
      const path: (string | number)[] = [...aliasPath, intent.field];
      if (intent.value === '') {
        doc.deleteIn(path);
      } else {
        setScalar(doc, path, intent.value);
      }
      break;
    }

    default:
      break;
  }

  return { doc, model: fromPlainObject(doc.toJS()), parseError: null };
}

function tasksSeq(doc: Document): YAMLSeq | null {
  const tasks = doc.get('tasks');
  return isSeq(tasks) ? tasks : null;
}

function ensureTasksSeq(doc: Document): YAMLSeq {
  const existing = tasksSeq(doc);
  if (existing) {
    return existing;
  }
  const seq = doc.createNode([]) as YAMLSeq;
  doc.set('tasks', seq);
  return seq;
}

function tasksItems(doc: Document): YAMLMap[] {
  return (tasksSeq(doc)?.items ?? []).filter(isMap);
}

function taskAt(doc: Document, index: number): YAMLMap | null {
  const item = tasksSeq(doc)?.items[index];
  return isMap(item) ? item : null;
}

function taskIndexByName(doc: Document, name: string): number {
  const items = tasksSeq(doc)?.items ?? [];
  return items.findIndex((item) => isMap(item) && scalarText(item.get('name')) === name);
}

function scalarText(node: unknown): string {
  // ``YAMLMap.get`` already unwraps scalars (unlike ``seq.items``, which
  // hands back nodes), so accept both shapes here.
  const value = isScalar(node) ? node.value : node;
  if (typeof value === 'string') {
    return value;
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  return '';
}

/**
 * Set a scalar **in place** when the key already exists, so its comment and
 * style survive; otherwise create the key.
 */
function setScalar(
  doc: Document,
  path: (string | number)[],
  value: string,
): void {
  const existing = doc.getIn(path, true);
  if (isScalar(existing)) {
    existing.value = value;
    return;
  }
  doc.setIn(path, value);
}
