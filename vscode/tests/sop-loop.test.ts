import { strict as assert } from 'node:assert';
import { test } from 'node:test';

import { issues, wouldCycle } from '../src/core/sop/graph';
import { applyIntentToModel } from '../src/core/sop/model-ops';
import { fromPlainObject } from '../src/core/sop/model';
import { applyIntent, parseSopText, serializeSop } from '../src/core/sop/yaml-doc';

const LOOP_YAML = `name: review-fix-loop
loops:
  - name: refine
    nodes: [review, fix]
    entry: review
    max_iterations: 3
tasks:
  - name: review
    input: Review
    output: review-report
    depends_on: [fix]
  - name: fix
    input: Apply \${review.review-report}
    depends_on: [review]
    output: fix-result
`;

const ACYCLIC_YAML = `name: review-fix
tasks:
  - name: review
    input: Review
    output: review-report
  - name: fix
    input: Apply \${review.review-report}
    depends_on: [review]
    output: fix-result
`;

test('parses loop metadata', () => {
  const doc = parseSopText(LOOP_YAML).model;
  assert.equal(doc.loops.length, 1);
  assert.deepEqual(doc.loops[0], {
    name: 'refine',
    nodes: ['review', 'fix'],
    maxIterations: 3,
    entry: 'review',
  });
});

test('allows a cycle inside a declared loop', () => {
  const doc = parseSopText(LOOP_YAML).model;
  const cycleErrors = issues(doc).filter((issue) => issue.message.includes('cycle'));
  assert.equal(cycleErrors.length, 0);
});

test('rejects an undeclared cycle', () => {
  const doc = fromPlainObject({
    name: 'bad',
    identities: {},
    tasks: [
      {
        name: 'a',
        identity: '',
        depends_on: ['b'],
        input: '',
        output: '',
        provider: '',
        daemon: '',
        dir: '',
        index: 0,
      },
      {
        name: 'b',
        identity: '',
        depends_on: ['a'],
        input: '',
        output: '',
        provider: '',
        daemon: '',
        dir: '',
        index: 1,
      },
    ],
    loops: [],
  });
  assert.ok(issues(doc).some((issue) => issue.message.includes('cycle')));
});

test('wouldCycle permits intra-loop closure', () => {
  const doc = parseSopText(LOOP_YAML).model;
  assert.equal(wouldCycle(doc, 'fix', 'review'), false);
});

test('YAML edits stay in sync with model edits', () => {
  const parsed = parseSopText(LOOP_YAML);
  const renamed = applyIntent(parsed, { kind: 'renameNode', from: 'review', to: 'audit' });
  const modelRenamed = applyIntentToModel(parsed.model, {
    kind: 'renameNode',
    from: 'review',
    to: 'audit',
  });
  const roundTripped = parseSopText(serializeSop(renamed)).model;
  assert.deepEqual(roundTripped, modelRenamed);
  assert.deepEqual(roundTripped.loops[0].nodes, ['audit', 'fix']);
  assert.equal(roundTripped.loops[0].entry, 'audit');
});

test('add, edit, and delete loop metadata round-trips', () => {
  const parsed = parseSopText(LOOP_YAML);
  const withLoop = applyIntent(parsed, {
    kind: 'addLoop',
    name: 'verify',
    nodes: ['review', 'fix'],
  });
  const withMembers = applyIntent(withLoop, {
    kind: 'setLoopNodes',
    name: 'verify',
    nodes: ['fix', 'review'],
  });
  const withMax = applyIntent(withMembers, {
    kind: 'setLoopField',
    name: 'verify',
    field: 'maxIterations',
    value: 5,
  });
  const doc = parseSopText(serializeSop(withMax)).model;
  const verify = doc.loops.find((loop) => loop.name === 'verify');
  assert.ok(verify);
  assert.deepEqual(verify.nodes, ['fix', 'review']);
  assert.equal(verify.maxIterations, 5);
  assert.equal(verify.entry, 'review');
});

test('deleting a node updates loop membership and entry', () => {
  const parsed = parseSopText(LOOP_YAML);
  const model = applyIntentToModel(parsed.model, {
    kind: 'deleteNode',
    name: 'review',
  });
  const yamlModel = parseSopText(
    serializeSop(
      applyIntent(parsed, { kind: 'deleteNode', name: 'review' }),
    ),
  ).model;
  assert.deepEqual(yamlModel, model);
  assert.deepEqual(yamlModel.loops, []);
});

test('drawing a back edge infers a loop declaration', () => {
  const parsed = parseSopText(ACYCLIC_YAML);
  const expected = applyIntentToModel(parsed.model, {
    kind: 'addDependency',
    node: 'review',
    dependsOn: 'fix',
  });
  const actual = parseSopText(
    serializeSop(
      applyIntent(parsed, {
        kind: 'addDependency',
        node: 'review',
        dependsOn: 'fix',
      }),
    ),
  ).model;
  assert.deepEqual(actual, expected);
  assert.deepEqual(actual.loops, [
    { name: 'loop-1', nodes: ['review', 'fix'], maxIterations: 2, entry: 'review' },
  ]);
  assert.equal(issues(actual).filter((issue) => issue.message.includes('cycle')).length, 0);
});

test('removing the closing edge removes the inferred loop', () => {
  const parsed = parseSopText(LOOP_YAML);
  const expected = applyIntentToModel(parsed.model, {
    kind: 'removeDependency',
    node: 'review',
    dependsOn: 'fix',
  });
  const actual = parseSopText(
    serializeSop(
      applyIntent(parsed, {
        kind: 'removeDependency',
        node: 'review',
        dependsOn: 'fix',
      }),
    ),
  ).model;
  assert.deepEqual(actual, expected);
  assert.deepEqual(actual.loops, []);
});
