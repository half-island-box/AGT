import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { AgtApi } from './api';
import type { ServerState } from './types';

export interface LifecycleOptions {
  serverUrl: string;
  startCommand: string;
  stopCommand: string;
  autoStart: boolean;
  /** Bring the local daemon up whenever this class starts the server. */
  autoStartDaemon: boolean;
  daemonStartCommand: string;
  daemonStopCommand: string;
  /** Daemon pid file; defaults to `~/.agt/daemon.pid` (same as the CLI). */
  daemonPidFile?: string;
  logger?: (message: string) => void;
}

/**
 * Owns the health state machine for the Python backend.
 *
 * The server itself is always a detached process managed by the `agt` CLI
 * (pid file based); this class only probes it and shells out to the CLI's
 * start/stop commands when needed. Kept free of `vscode` imports so the
 * same logic can be reused by other host shells (e.g. Obsidian).
 */
export class ServerLifecycle {
  readonly api: AgtApi;
  state: ServerState = 'unknown';
  latencyMs: number | null = null;

  constructor(private options: LifecycleOptions) {
    this.api = new AgtApi(options.serverUrl);
  }

  updateOptions(options: Partial<LifecycleOptions>): void {
    this.options = { ...this.options, ...options };
    if (options.serverUrl !== undefined) {
      this.api.setBaseUrl(options.serverUrl);
    }
  }

  private log(message: string): void {
    this.options.logger?.(message);
  }

  async probe(): Promise<boolean> {
    const latency = await this.api.ping();
    this.latencyMs = latency;
    if (latency !== null) {
      this.state = 'online';
      return true;
    }
    this.state = 'offline';
    return false;
  }

  /** Probe; when offline and autoStart is enabled, start it and wait. */
  async ensureStarted(): Promise<boolean> {
    if (await this.probe()) {
      return true;
    }
    if (!this.options.autoStart || !this.options.startCommand.trim()) {
      return false;
    }
    this.state = 'starting';
    this.log(`starting server: ${this.options.startCommand}`);
    try {
      await this.runShell(this.options.startCommand);
    } catch (err) {
      this.log(`start command failed: ${String(err)}`);
    }
    // `--detach` returns before uvicorn is listening; poll for readiness.
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      if (await this.probe()) {
        return true;
      }
    }
    this.state = 'offline';
    return false;
  }

  async stop(): Promise<boolean> {
    if (!this.options.stopCommand.trim()) {
      return false;
    }
    this.log(`stopping server: ${this.options.stopCommand}`);
    try {
      await this.runShell(this.options.stopCommand);
    } catch (err) {
      this.log(`stop command failed: ${String(err)}`);
      return false;
    }
    for (let i = 0; i < 10; i++) {
      await sleep(300);
      if (!(await this.probe())) {
        this.state = 'offline';
        return true;
      }
    }
    return false;
  }

  /** True when the local daemon pid file names a live daemon process. */
  daemonRunning(): boolean {
    return readLivePid(this.daemonPidFile) !== null;
  }

  /**
   * Start the local daemon unless one is already running.
   *
   * An already-running daemon is left alone: `agt daemon start` replaces
   * the existing process, which would kill tasks it is executing. The
   * configured `serverUrl` is exported as `AGT_SERVER_URL` so the daemon
   * registers with the server this client talks to.
   */
  async startDaemon(): Promise<boolean> {
    if (this.daemonRunning()) {
      return true;
    }
    if (!this.options.daemonStartCommand.trim()) {
      return false;
    }
    this.log(`starting daemon: ${this.options.daemonStartCommand}`);
    try {
      await this.runShell(this.options.daemonStartCommand, {
        env: { AGT_SERVER_URL: this.options.serverUrl },
      });
    } catch (err) {
      this.log(`daemon start command failed: ${String(err)}`);
    }
    // `--detach` returns before the child writes its pid file.
    for (let i = 0; i < 60; i++) {
      await sleep(500);
      if (this.daemonRunning()) {
        return true;
      }
    }
    return false;
  }

  /** Start the local daemon alongside the server, honoring the switch. */
  async ensureDaemonStarted(): Promise<boolean> {
    if (this.daemonRunning()) {
      return true;
    }
    if (!this.options.autoStartDaemon) {
      return false;
    }
    return this.startDaemon();
  }

  async stopDaemon(): Promise<boolean> {
    if (!this.daemonRunning()) {
      return true;
    }
    if (!this.options.daemonStopCommand.trim()) {
      return false;
    }
    this.log(`stopping daemon: ${this.options.daemonStopCommand}`);
    try {
      await this.runShell(this.options.daemonStopCommand);
    } catch (err) {
      this.log(`daemon stop command failed: ${String(err)}`);
      return false;
    }
    for (let i = 0; i < 20; i++) {
      await sleep(300);
      if (!this.daemonRunning()) {
        return true;
      }
    }
    return false;
  }

  private get daemonPidFile(): string {
    return this.options.daemonPidFile ?? join(homedir(), '.agt', 'daemon.pid');
  }

  private runShell(
    command: string,
    opts: { env?: Record<string, string>; timeoutMs?: number } = {},
  ): Promise<void> {
    const timeoutMs = opts.timeoutMs ?? 15000;
    return new Promise((resolve, reject) => {
      const child = spawn(command, {
        shell: true,
        stdio: 'ignore',
        detached: false,
        env: opts.env ? { ...process.env, ...opts.env } : process.env,
      });
      const timer = setTimeout(() => {
        child.kill();
        resolve();
      }, timeoutMs);
      child.once('error', (err) => {
        clearTimeout(timer);
        reject(err);
      });
      child.once('close', () => {
        clearTimeout(timer);
        resolve();
      });
    });
  }
}

/**
 * Read a pid file and return the pid only when a daemon still owns it.
 *
 * Mirrors `agt.daemon.main._read_pidfile` / `_pidfile_alive`: the pid is
 * checked for liveness with signal 0 and, on Linux, against `/proc` so a
 * recycled pid belonging to an unrelated process is not mistaken for the
 * daemon.
 */
function readLivePid(pidFile: string): number | null {
  let raw: string;
  try {
    raw = readFileSync(pidFile, 'utf8');
  } catch {
    return null;
  }
  const pid = Number.parseInt(raw.trim(), 10);
  if (!Number.isInteger(pid) || pid <= 0) {
    return null;
  }
  try {
    process.kill(pid, 0);
  } catch {
    return null;
  }
  try {
    const cmdline = readFileSync(`/proc/${pid}/cmdline`, 'utf8');
    return cmdline.includes('agt') && cmdline.includes('daemon') ? pid : null;
  } catch {
    return pid; // Non-Linux host: trust the pid file.
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
