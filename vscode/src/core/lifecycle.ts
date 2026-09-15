import { spawn } from 'node:child_process';
import { AgtApi } from './api';
import type { ServerState } from './types';

export interface LifecycleOptions {
  serverUrl: string;
  startCommand: string;
  stopCommand: string;
  autoStart: boolean;
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

  private runShell(command: string, timeoutMs = 15000): Promise<void> {
    return new Promise((resolve, reject) => {
      const child = spawn(command, {
        shell: true,
        stdio: 'ignore',
        detached: false,
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
