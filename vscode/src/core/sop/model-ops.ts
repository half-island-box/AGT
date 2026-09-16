/**
 * Intent semantics expressed over the **model** (pure, no YAML).
 *
 * Two callers depend on this:
 *
 * - the browser preview mock, which has no YAML document at hand;
 * - the round-trip tests, which assert that applying an intent to the
 *   real YAML document yields the same model as this function — that
 *   equality is what keeps the mock from drifting away from the editor.
 */

import { uniqueNodeName, rewritePlaceholders } from './graph';
import type { EditIntent, SopDoc, SopNode } from './model';

function withNode(
  doc: SopDoc,
  name: string,
  update: (node: SopNode) => SopNode,
): SopDoc {
  return {
    ...doc,
    nodes: doc.nodes.map((node) => (node.name === name ? update(node) : node)),
  };
}

/** Normalize indices after insert/delete so ``index`` keeps matching YAML. */
function reindex(doc: SopDoc): SopDoc {
  return { ...doc, nodes: doc.nodes.map((node, index) => ({ ...node, index })) };
}

export function applyIntentToModel(doc: SopDoc, intent: EditIntent): SopDoc {
  switch (intent.kind) {
    case 'setName':
      return { ...doc, name: intent.name };

    case 'addNode': {
      const name = intent.name?.trim() || uniqueNodeName(doc);
      const after = intent.after ?? '';
      const node: SopNode = {
        name,
        identity: '',
        dependsOn: after !== '' ? [after] : [],
        input: '',
        output: '',
        provider: '',
        daemon: '',
        dir: '',
        index: doc.nodes.length,
      };
      return reindex({ ...doc, nodes: [...doc.nodes, node] });
    }

    case 'deleteNode': {
      const nodes = doc.nodes
        .filter((node) => node.name !== intent.name)
        .map((node) => ({
          ...node,
          dependsOn: node.dependsOn.filter((dep) => dep !== intent.name),
        }));
      return reindex({ ...doc, nodes });
    }

    case 'renameNode': {
      if (intent.from === intent.to) {
        return doc;
      }
      const nodes = doc.nodes.map((node) => {
        const renamed =
          node.name === intent.from ? { ...node, name: intent.to } : node;
        return {
          ...renamed,
          dependsOn: renamed.dependsOn.map((dep) =>
            dep === intent.from ? intent.to : dep,
          ),
          input: rewritePlaceholders(renamed.input, intent.from, intent.to),
        };
      });
      return { ...doc, nodes };
    }

    case 'setNodeField':
      return withNode(doc, intent.name, (node) => ({
        ...node,
        [intent.field]: intent.value,
      }));

    case 'addDependency':
      return withNode(doc, intent.node, (node) =>
        node.dependsOn.includes(intent.dependsOn)
          ? node
          : { ...node, dependsOn: [...node.dependsOn, intent.dependsOn] },
      );

    case 'removeDependency':
      return withNode(doc, intent.node, (node) => ({
        ...node,
        dependsOn: node.dependsOn.filter((dep) => dep !== intent.dependsOn),
      }));

    case 'addIdentity': {
      if (doc.identities.some((i) => i.alias === intent.alias)) {
        return doc;
      }
      return {
        ...doc,
        identities: [
          ...doc.identities,
          {
            alias: intent.alias,
            did: intent.did ?? '',
            provider: intent.provider ?? '',
          },
        ],
      };
    }

    case 'removeIdentity': {
      const identities = doc.identities.filter((i) => i.alias !== intent.alias);
      // Referencing nodes keep the alias but go red in the problems panel
      // (mirrors the delete-node rule: never rewrite a prompt silently).
      return { ...doc, identities };
    }

    case 'setIdentityField':
      return {
        ...doc,
        identities: doc.identities.map((identity) =>
          identity.alias === intent.alias
            ? { ...identity, [intent.field]: intent.value }
            : identity,
        ),
      };

    default:
      return doc;
  }
}
