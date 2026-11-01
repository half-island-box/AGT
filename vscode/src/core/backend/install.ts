import { venvPythonPath } from './detect';

/** Which tool will create the private environment. */
export type InstallTool = 'uv' | 'python';

export interface PythonRuntime {
  tool: InstallTool;
  cmd: string;
  /** Arguments that must precede ``-c``/``-m`` (e.g. ``py -3.12``). */
  prefixArgs: string[];
}

export interface InstallStep {
  label: 'createEnv' | 'install';
  cmd: string;
  args: string[];
}

export interface InstallPlan {
  steps: InstallStep[];
  venvPython: string;
}

export const MIN_PYTHON: readonly [number, number] = [3, 12];

/** Python launchers to try, most specific first. ``uv`` is checked separately. */
export function pythonCandidates(
  platform: NodeJS.Platform,
): Array<{ cmd: string; prefixArgs: string[] }> {
  if (platform === 'win32') {
    return [
      { cmd: 'py', prefixArgs: ['-3.12'] },
      { cmd: 'python3.12', prefixArgs: [] },
      { cmd: 'python', prefixArgs: [] },
    ];
  }
  return [
    { cmd: 'python3.12', prefixArgs: [] },
    { cmd: 'python3', prefixArgs: [] },
    { cmd: 'python', prefixArgs: [] },
  ];
}

/** Parse ``3.12`` from the version probe's stdout. */
export function parsePythonVersion(output: string): [number, number] | null {
  const match = /(\d+)\.(\d+)/.exec(output.trim());
  if (!match) return null;
  return [Number.parseInt(match[1], 10), Number.parseInt(match[2], 10)];
}

export function supportsPython312(version: [number, number]): boolean {
  return (
    version[0] > MIN_PYTHON[0] ||
    (version[0] === MIN_PYTHON[0] && version[1] >= MIN_PYTHON[1])
  );
}

/** Only pin versions that look like a real release, never arbitrary text. */
export function pipSpec(version: string | null | undefined): string {
  return version && /^\d+\.\d+\.\d+([-+][0-9A-Za-z.+-]+)?$/.test(version)
    ? `agt-team==${version}`
    : 'agt-team';
}

/**
 * Commands that create the private venv and install ``agt-team`` into it.
 *
 * ``uv`` is preferred when present: besides being much faster, ``uv venv
 * --python 3.12`` can fetch a managed Python when the machine has none.
 */
export function buildInstallPlan(
  python: PythonRuntime,
  platform: NodeJS.Platform,
  venvDir: string,
  version?: string | null,
): InstallPlan {
  const venvPython = venvPythonPath(platform, venvDir);
  const spec = pipSpec(version);
  if (python.tool === 'uv') {
    return {
      venvPython,
      steps: [
        {
          label: 'createEnv',
          cmd: python.cmd,
          args: [...python.prefixArgs, 'venv', '--python', '3.12', venvDir],
        },
        {
          label: 'install',
          cmd: python.cmd,
          args: [...python.prefixArgs, 'pip', 'install', '--python', venvPython, spec],
        },
      ],
    };
  }
  return {
    venvPython,
    steps: [
      {
        label: 'createEnv',
        cmd: python.cmd,
        args: [...python.prefixArgs, '-m', 'venv', venvDir],
      },
      {
        label: 'install',
        cmd: venvPython,
        args: [
          '-m',
          'pip',
          'install',
          '--disable-pip-version-check',
          '--no-input',
          spec,
        ],
      },
    ],
  };
}
