import { spawn } from 'node:child_process';

export interface RunResult {
  code: number | null;
  stdout: string;
  stderr: string;
  timedOut: boolean;
  aborted: boolean;
}

export interface RunOptions {
  cwd?: string;
  env?: NodeJS.ProcessEnv;
  timeoutMs?: number;
  signal?: AbortSignal;
  /** Keep only the tail of each stream; pip/uv can be verbose. */
  maxOutput?: number;
}

const DEFAULT_MAX_OUTPUT = 64_000;

function append(current: string, chunk: string, limit: number): string {
  const next = current + chunk;
  return next.length > limit ? next.slice(next.length - limit) : next;
}

function environment(opts: RunOptions): NodeJS.ProcessEnv {
  return opts.env ? { ...process.env, ...opts.env } : process.env;
}

function collect(
  child: ReturnType<typeof spawn>,
  opts: RunOptions,
): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const limit = opts.maxOutput ?? DEFAULT_MAX_OUTPUT;
    let stdout = '';
    let stderr = '';
    let timedOut = false;
    let aborted = false;
    let settled = false;

    child.stdout?.on('data', (chunk: Buffer | string) => {
      stdout = append(stdout, String(chunk), limit);
    });
    child.stderr?.on('data', (chunk: Buffer | string) => {
      stderr = append(stderr, String(chunk), limit);
    });

    const finish = (code: number | null): void => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
      resolve({ code, stdout, stderr, timedOut, aborted });
    };
    const kill = (): void => {
      child.kill('SIGTERM');
      const escalation = setTimeout(() => {
        if (!settled) child.kill('SIGKILL');
      }, 2000);
      escalation.unref();
    };
    function onAbort(): void {
      aborted = true;
      kill();
    }

    if (opts.signal) {
      if (opts.signal.aborted) {
        onAbort();
      } else {
        opts.signal.addEventListener('abort', onAbort, { once: true });
      }
    }
    const timer = opts.timeoutMs
      ? setTimeout(() => {
          timedOut = true;
          kill();
        }, opts.timeoutMs)
      : undefined;

    child.once('error', (err) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      opts.signal?.removeEventListener('abort', onAbort);
      reject(err);
    });
    child.once('close', (code) => finish(code));
  });
}

/**
 * Run a program directly (no shell), capturing its output.
 *
 * Args are passed as an array so paths containing spaces stay intact on every
 * platform. Rejects only when the process cannot be spawned at all; a non-zero
 * exit code is reported in ``code``.
 */
export function runProcess(
  command: string,
  args: string[],
  opts: RunOptions = {},
): Promise<RunResult> {
  const child = spawn(command, args, {
    cwd: opts.cwd,
    env: environment(opts),
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  return collect(child, opts);
}

/**
 * Run a user-configured shell command line. Lifecycle commands are arbitrary
 * strings (``uv run agt server start --detach``), so they need the shell.
 */
export function runShellCommand(
  command: string,
  opts: RunOptions = {},
): Promise<RunResult> {
  const child = spawn(command, {
    cwd: opts.cwd,
    env: environment(opts),
    shell: true,
    stdio: ['ignore', 'pipe', 'pipe'],
    windowsHide: true,
  });
  return collect(child, opts);
}
