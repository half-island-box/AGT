import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { join } from 'node:path';

import {
  compareVersions,
  parseAgtVersion,
  quoteArg,
  resolveCommands,
  venvAgtPath,
  venvPythonPath,
  type ConfiguredCommands,
} from '../src/core/backend/detect';
import {
  buildInstallPlan,
  parsePythonVersion,
  pipSpec,
  pythonCandidates,
  supportsPython312,
} from '../src/core/backend/install';
import { parseLatestVersion } from '../src/core/backend/pypi';

const NO_COMMANDS: ConfiguredCommands = {
  startCommand: '',
  stopCommand: '',
  daemonStartCommand: '',
  daemonStopCommand: '',
};

test('parseAgtVersion reads click version output', () => {
  assert.equal(parseAgtVersion('agt, version 0.1.5\n'), '0.1.5');
  assert.equal(parseAgtVersion('agt version 1.2.3-rc.1'), '1.2.3-rc.1');
  assert.equal(parseAgtVersion('command not found'), null);
});

test('compareVersions orders numeric segments', () => {
  assert.equal(compareVersions('0.1.5', '0.1.10'), -1);
  assert.equal(compareVersions('0.2.0', '0.1.9'), 1);
  assert.equal(compareVersions('0.1.5', '0.1.5'), 0);
  assert.equal(compareVersions('1.0.0-rc.1', '1.0.0'), -1);
  assert.equal(compareVersions('1.0.0+build2', '1.0.0'), 0);
});

test('venv paths follow the platform layout', () => {
  assert.equal(venvAgtPath('darwin', '/v'), join('/v', 'bin', 'agt'));
  assert.equal(venvAgtPath('win32', '/v'), join('/v', 'Scripts', 'agt.exe'));
  assert.equal(venvPythonPath('linux', '/v'), join('/v', 'bin', 'python'));
  assert.equal(venvPythonPath('win32', '/v'), join('/v', 'Scripts', 'python.exe'));
});

test('quoteArg survives spaces and quotes', () => {
  assert.equal(quoteArg('/opt/agt', 'linux'), "'/opt/agt'");
  assert.equal(quoteArg("/a b/it's", 'darwin'), `'/a b/it'\\''s'`);
  assert.equal(quoteArg('C:\\Program Files\\agt.exe', 'win32'), '"C:\\Program Files\\agt.exe"');
});

test('resolveCommands derives commands from the detected backend', () => {
  const resolved = resolveCommands(
    NO_COMMANDS,
    { kind: 'managed', path: '/opt/agt', version: '0.1.5' },
    'linux',
  );
  assert.equal(resolved.startCommand, "'/opt/agt' server start --detach");
  assert.equal(resolved.stopCommand, "'/opt/agt' server stop");
  assert.equal(resolved.daemonStartCommand, "'/opt/agt' daemon start --detach");
  assert.equal(resolved.daemonStopCommand, "'/opt/agt' daemon stop");
  assert.equal(resolved.startCwd, undefined);
});

test('resolveCommands keeps configured commands and workspace cwd', () => {
  const configured: ConfiguredCommands = {
    ...NO_COMMANDS,
    startCommand: 'custom start',
    stopCommand: 'custom stop',
    daemonStartCommand: 'custom daemon start',
    daemonStopCommand: 'custom daemon stop',
  };
  const resolved = resolveCommands(
    configured,
    { kind: 'workspace', path: '/repo', version: '0.1.5' },
    'linux',
  );
  assert.equal(resolved.startCommand, 'custom start');
  assert.equal(resolved.daemonStopCommand, 'custom daemon stop');
  // Nothing is derived, so the workspace cwd is not forced on the run.
  assert.equal(resolved.startCwd, undefined);

  // One derived command is enough to need the checkout as cwd.
  const mixed = resolveCommands(
    { ...configured, stopCommand: '' },
    { kind: 'workspace', path: '/repo', version: '0.1.5' },
    'linux',
  );
  assert.equal(mixed.stopCommand, 'uv run agt server stop');
  assert.equal(mixed.startCwd, '/repo');
});

test('resolveCommands uses the workspace root with uv run', () => {
  const resolved = resolveCommands(
    NO_COMMANDS,
    { kind: 'workspace', path: '/repo', version: '0.1.5' },
    'linux',
  );
  assert.equal(resolved.startCommand, 'uv run agt server start --detach');
  assert.equal(resolved.startCwd, '/repo');
});

test('resolveCommands returns empty commands with no backend', () => {
  const resolved = resolveCommands(NO_COMMANDS, null, 'linux');
  assert.equal(resolved.startCommand, '');
  assert.equal(resolved.stopCommand, '');
  assert.equal(resolved.startCwd, undefined);
});

test('pythonCandidates prefers the versioned launchers', () => {
  assert.equal(pythonCandidates('linux')[0].cmd, 'python3.12');
  assert.deepEqual(pythonCandidates('win32')[0], { cmd: 'py', prefixArgs: ['-3.12'] });
});

test('Python version probe parsing', () => {
  assert.deepEqual(parsePythonVersion('3.12\n'), [3, 12]);
  assert.deepEqual(parsePythonVersion('3.13.1'), [3, 13]);
  assert.equal(parsePythonVersion('nope'), null);
  assert.equal(supportsPython312([3, 12]), true);
  assert.equal(supportsPython312([3, 13]), true);
  assert.equal(supportsPython312([3, 11]), false);
  assert.equal(supportsPython312([2, 7]), false);
});

test('pipSpec pins only well-formed versions', () => {
  assert.equal(pipSpec('0.1.5'), 'agt-team==0.1.5');
  assert.equal(pipSpec('0.1.5-rc.1'), 'agt-team==0.1.5-rc.1');
  assert.equal(pipSpec(null), 'agt-team');
  assert.equal(pipSpec('0.1.5; rm -rf /'), 'agt-team');
});

test('buildInstallPlan with uv', () => {
  const plan = buildInstallPlan(
    { tool: 'uv', cmd: 'uv', prefixArgs: [] },
    'linux',
    '/private/venv',
    '0.1.5',
  );
  const python = join('/private/venv', 'bin', 'python');
  assert.deepEqual(plan.steps, [
    { label: 'createEnv', cmd: 'uv', args: ['venv', '--python', '3.12', '/private/venv'] },
    { label: 'install', cmd: 'uv', args: ['pip', 'install', '--python', python, 'agt-team==0.1.5'] },
  ]);
  assert.equal(plan.venvPython, python);
});

test('buildInstallPlan with system Python', () => {
  const plan = buildInstallPlan(
    { tool: 'python', cmd: 'python3.12', prefixArgs: [] },
    'linux',
    '/private/venv',
    null,
  );
  const python = join('/private/venv', 'bin', 'python');
  assert.deepEqual(plan.steps, [
    { label: 'createEnv', cmd: 'python3.12', args: ['-m', 'venv', '/private/venv'] },
    {
      label: 'install',
      cmd: python,
      args: ['-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', 'agt-team'],
    },
  ]);
});

test('parseLatestVersion reads the PyPI JSON shape', () => {
  assert.equal(parseLatestVersion({ info: { version: '0.1.5' } }), '0.1.5');
  assert.equal(parseLatestVersion({ info: { version: ' 0.1.6 ' } }), '0.1.6');
  assert.equal(parseLatestVersion({ info: {} }), null);
  assert.equal(parseLatestVersion({}), null);
  assert.equal(parseLatestVersion(null), null);
  assert.equal(parseLatestVersion('0.1.5'), null);
});
