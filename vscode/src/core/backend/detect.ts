import { join } from 'node:path';

/** Where a usable ``agt`` executable came from. */
export type BackendKind = 'managed' | 'path' | 'workspace';

export interface BackendInfo {
  kind: BackendKind;
  /**
   * Absolute executable path for ``managed``/``path``; the workspace root for
   * ``workspace`` (where ``agt`` is run through ``uv run``).
   */
  path: string;
  version: string;
}

export interface ConfiguredCommands {
  startCommand: string;
  stopCommand: string;
  daemonStartCommand: string;
  daemonStopCommand: string;
}

export interface ResolvedCommands extends ConfiguredCommands {
  /** Directory the commands must run in (only ``uv run`` needs one). */
  startCwd?: string;
}

const VERSION_RE = /version\s+(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)/i;
const BARE_VERSION_RE = /(\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.+-]+)?)/;

/** ``agt, version 0.1.5`` → ``0.1.5``; anything unparseable → ``null``. */
export function parseAgtVersion(output: string): string | null {
  const match = VERSION_RE.exec(output) ?? BARE_VERSION_RE.exec(output);
  return match ? match[1] : null;
}

function splitVersion(version: string): { nums: number[]; pre: string | null } {
  const withoutBuild = version.split('+')[0];
  const dash = withoutBuild.indexOf('-');
  const core = dash === -1 ? withoutBuild : withoutBuild.slice(0, dash);
  const pre = dash === -1 ? null : withoutBuild.slice(dash + 1);
  return {
    nums: core.split('.').map((part) => Number.parseInt(part, 10) || 0),
    pre,
  };
}

/** Standard semver-ish ordering, enough to answer "is PyPI newer?". */
export function compareVersions(a: string, b: string): number {
  const left = splitVersion(a);
  const right = splitVersion(b);
  const length = Math.max(left.nums.length, right.nums.length);
  for (let i = 0; i < length; i++) {
    const l = left.nums[i] ?? 0;
    const r = right.nums[i] ?? 0;
    if (l !== r) return l < r ? -1 : 1;
  }
  if (left.pre === right.pre) return 0;
  if (left.pre === null) return 1;
  if (right.pre === null) return -1;
  return left.pre < right.pre ? -1 : 1;
}

export function venvBinDir(platform: NodeJS.Platform, venvDir: string): string {
  return join(venvDir, platform === 'win32' ? 'Scripts' : 'bin');
}

export function venvAgtPath(platform: NodeJS.Platform, venvDir: string): string {
  return join(venvBinDir(platform, venvDir), platform === 'win32' ? 'agt.exe' : 'agt');
}

export function venvPythonPath(platform: NodeJS.Platform, venvDir: string): string {
  return join(venvBinDir(platform, venvDir), platform === 'win32' ? 'python.exe' : 'python');
}

/** Quote one argument for the shell that ``ServerLifecycle`` will use. */
export function quoteArg(value: string, platform: NodeJS.Platform): string {
  if (platform === 'win32') {
    return `"${value.replace(/"/g, '\\"')}"`;
  }
  return `'${value.replace(/'/g, `'\\''`)}'`;
}

function commandsFor(info: BackendInfo, platform: NodeJS.Platform): ConfiguredCommands {
  if (info.kind === 'workspace') {
    return {
      startCommand: 'uv run agt server start --detach',
      stopCommand: 'uv run agt server stop',
      daemonStartCommand: 'uv run agt daemon start --detach',
      daemonStopCommand: 'uv run agt daemon stop',
    };
  }
  const exe = quoteArg(info.path, platform);
  return {
    startCommand: `${exe} server start --detach`,
    stopCommand: `${exe} server stop`,
    daemonStartCommand: `${exe} daemon start --detach`,
    daemonStopCommand: `${exe} daemon stop`,
  };
}

/**
 * Merge user-configured commands with what detection found.
 *
 * A non-empty configured command always wins (existing setups keep working);
 * otherwise the command is derived from the detected backend. With no backend
 * at all every command is empty, which the lifecycle treats as "do nothing"
 * instead of failing on a command that cannot exist.
 */
export function resolveCommands(
  configured: ConfiguredCommands,
  detected: BackendInfo | null,
  platform: NodeJS.Platform,
): ResolvedCommands {
  const derived = detected ? commandsFor(detected, platform) : null;
  const pick = (configuredValue: string, key: keyof ConfiguredCommands): string =>
    configuredValue.trim() || derived?.[key] || '';
  // Any command still coming from detection needs the checkout as its cwd;
  // configured commands run wherever the extension host happens to be.
  const usesDerivedCommand =
    !configured.startCommand.trim() ||
    !configured.stopCommand.trim() ||
    !configured.daemonStartCommand.trim() ||
    !configured.daemonStopCommand.trim();
  return {
    startCommand: pick(configured.startCommand, 'startCommand'),
    stopCommand: pick(configured.stopCommand, 'stopCommand'),
    daemonStartCommand: pick(configured.daemonStartCommand, 'daemonStartCommand'),
    daemonStopCommand: pick(configured.daemonStopCommand, 'daemonStopCommand'),
    startCwd:
      detected?.kind === 'workspace' && usesDerivedCommand ? detected.path : undefined,
  };
}
