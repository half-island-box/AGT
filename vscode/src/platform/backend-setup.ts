import * as vscode from 'vscode';
import { mkdir, readFile } from 'node:fs/promises';
import { join } from 'node:path';
import { t } from '../core/i18n';
import type { ServerLifecycle } from '../core/lifecycle';
import { runProcess, type RunResult } from '../core/process';
import {
  parseAgtVersion,
  resolveCommands,
  venvAgtPath,
  type BackendInfo,
} from '../core/backend/detect';
import {
  buildInstallPlan,
  parsePythonVersion,
  pythonCandidates,
  supportsPython312,
  type PythonRuntime,
} from '../core/backend/install';
import { fetchLatestVersion } from '../core/backend/pypi';
import type { UiLanguage } from '../core/types';
import type { BackendStatus } from './statusbar';

const PROMPTED_KEY = 'agt.backendPrompted';
const DOCS_URL = 'https://github.com/half-island-box/agent-team#quick-start';
const PYTHON_URL = 'https://www.python.org/downloads/';
const PROBE_TIMEOUT_MS = 5_000;
const INSTALL_TIMEOUT_MS = 300_000;

/** Settings `BackendSetup` reads; the rest of the config stays in the host. */
export interface BackendSetupConfig {
  startCommand: string;
  stopCommand: string;
  daemonStartCommand: string;
  daemonStopCommand: string;
  autoInstallBackend: 'prompt' | 'off';
  agtPath: string;
}

export interface BackendSetupOptions {
  context: vscode.ExtensionContext;
  lifecycle: ServerLifecycle;
  channel: vscode.OutputChannel;
  getConfig(): BackendSetupConfig;
  language(): UiLanguage;
  workspaceRoot(): string | undefined;
  setStatus(status: BackendStatus): void;
  /** Refresh the UI after a successful install (server + daemon start). */
  onInstalled(): void | Promise<void>;
}

/**
 * Finds (and, with explicit consent, installs) the `agt` backend.
 *
 * Nothing is installed without a modal confirmation: the "prompt" setting
 * only decides whether that dialog is offered automatically on first
 * activation or waits for the user to click the status bar entry.
 */
export class BackendSetup {
  private readonly backendDir: string;
  private readonly venvDir: string;

  constructor(private readonly opts: BackendSetupOptions) {
    this.backendDir = join(opts.context.globalStorageUri.fsPath, 'backend');
    this.venvDir = join(this.backendDir, 'venv');
  }

  /** Detect a usable backend and apply its commands to the lifecycle. */
  async initialize(): Promise<void> {
    const online = await this.opts.lifecycle.probe();
    const detected = await this.detect();
    this.apply(detected);
    if (online || detected) {
      this.opts.setStatus('ready');
      return;
    }
    this.opts.setStatus('missing');
    if (this.opts.getConfig().autoInstallBackend !== 'prompt') return;
    if (this.opts.context.globalState.get<boolean>(PROMPTED_KEY)) return;
    await this.promptInstall();
  }

  /** Re-run detection after the user changed paths/commands in Settings. */
  async reconfigure(): Promise<void> {
    const online = await this.opts.lifecycle.probe();
    const detected = await this.detect();
    this.apply(detected);
    this.opts.setStatus(online || detected ? 'ready' : 'missing');
  }

  /** Explicit user action (status bar / command): install if still missing. */
  async requestInstall(): Promise<void> {
    const detected = await this.detect();
    if (detected) {
      this.apply(detected);
      this.opts.setStatus('ready');
      const lang = this.opts.language();
      void vscode.window.showInformationMessage(
        t(lang, 'backend.alreadyInstalled', {
          version: detected.version,
          path: detected.path,
        }),
      );
      return;
    }
    await this.promptInstall();
  }

  private apply(detected: BackendInfo | null): void {
    const config = this.opts.getConfig();
    this.opts.lifecycle.updateOptions(
      resolveCommands(
        {
          startCommand: config.startCommand,
          stopCommand: config.stopCommand,
          daemonStartCommand: config.daemonStartCommand,
          daemonStopCommand: config.daemonStopCommand,
        },
        detected,
        process.platform,
      ),
    );
  }

  private async detect(): Promise<BackendInfo | null> {
    const configured = this.opts.getConfig().agtPath.trim();
    if (configured) {
      const info = await this.probe(configured, 'path');
      if (info) return info;
      this.log(`configured agtPath is not usable: ${configured}`);
    }
    const managed = await this.probe(
      venvAgtPath(process.platform, this.venvDir),
      'managed',
    );
    if (managed) return managed;
    const onPath = await this.probe('agt', 'path');
    if (onPath) return onPath;
    return this.probeWorkspace();
  }

  /** Run ``<command> --version``; a broken shim counts as "not found". */
  private async probe(
    command: string,
    kind: BackendInfo['kind'],
  ): Promise<BackendInfo | null> {
    try {
      const result = await runProcess(command, ['--version'], {
        timeoutMs: PROBE_TIMEOUT_MS,
      });
      if (result.code !== 0) return null;
      const version = parseAgtVersion(`${result.stdout}\n${result.stderr}`);
      return version ? { kind, path: command, version } : null;
    } catch {
      return null;
    }
  }

  /** Dev fallback: a checkout of this repo with ``uv`` available. */
  private async probeWorkspace(): Promise<BackendInfo | null> {
    const root = this.opts.workspaceRoot();
    if (!root) return null;
    try {
      const pyproject = await readFile(join(root, 'pyproject.toml'), 'utf8');
      if (!pyproject.includes('agt-team')) return null;
    } catch {
      return null;
    }
    try {
      const uv = await runProcess('uv', ['--version'], {
        timeoutMs: PROBE_TIMEOUT_MS,
      });
      if (uv.code !== 0) return null;
      const result = await runProcess('uv', ['run', 'agt', '--version'], {
        cwd: root,
        timeoutMs: 60_000,
      });
      if (result.code !== 0) return null;
      const version = parseAgtVersion(`${result.stdout}\n${result.stderr}`);
      return version ? { kind: 'workspace', path: root, version } : null;
    } catch {
      return null;
    }
  }

  /** The single gate in front of every install: a modal, user-initiated. */
  private async promptInstall(): Promise<void> {
    const lang = this.opts.language();
    await this.opts.context.globalState.update(PROMPTED_KEY, true);
    const latest = await fetchLatestVersion();
    if (!latest) {
      this.log('PyPI is unreachable; the installer will resolve the latest version itself.');
    }
    const detail = latest
      ? t(lang, 'backend.promptDetail', { version: latest, path: this.venvDir })
      : t(lang, 'backend.promptDetailUnknown', { path: this.venvDir });
    const installLabel = t(lang, 'backend.install');
    const docsLabel = t(lang, 'backend.docs');
    const choice = await vscode.window.showWarningMessage(
      t(lang, 'backend.promptTitle'),
      { modal: true, detail },
      installLabel,
      docsLabel,
    );
    if (choice === installLabel) {
      await this.install();
    } else if (choice === docsLabel) {
      void vscode.env.openExternal(vscode.Uri.parse(DOCS_URL));
    }
  }

  private async install(): Promise<void> {
    const lang = this.opts.language();
    const outcome: {
      info?: BackendInfo;
      failure?: string;
      noPython?: boolean;
      cancelled?: boolean;
      started?: boolean;
    } = {};
    this.opts.setStatus('installing');

    await vscode.window.withProgress(
      {
        location: vscode.ProgressLocation.Notification,
        title: t(lang, 'backend.progressTitle'),
        cancellable: true,
      },
      async (progress, token) => {
        const controller = new AbortController();
        const cancellation = token.onCancellationRequested(() => controller.abort());
        try {
          progress.report({ message: t(lang, 'backend.stepCheck') });
          const python = await this.findPython();
          if (!python) {
            outcome.noPython = true;
            return;
          }
          const latest = await fetchLatestVersion();
          await mkdir(this.backendDir, { recursive: true });
          const plan = buildInstallPlan(
            python,
            process.platform,
            this.venvDir,
            latest,
          );
          for (const step of plan.steps) {
            if (controller.signal.aborted) {
              outcome.cancelled = true;
              return;
            }
            progress.report({
              message:
                step.label === 'createEnv'
                  ? t(lang, 'backend.stepCreateEnv')
                  : latest
                    ? t(lang, 'backend.stepInstall', { version: latest })
                    : t(lang, 'backend.stepInstallUnknown'),
            });
            this.log(`$ ${step.cmd} ${step.args.join(' ')}`);
            const result = await runProcess(step.cmd, step.args, {
              timeoutMs: INSTALL_TIMEOUT_MS,
              signal: controller.signal,
              env: { PIP_DISABLE_PIP_VERSION_CHECK: '1' },
            });
            this.appendResult(result);
            if (result.aborted) {
              outcome.cancelled = true;
              return;
            }
            if (result.code !== 0) {
              outcome.failure = `${step.cmd} exited with ${result.code ?? 'no status'}${
                result.timedOut ? ' (timed out)' : ''
              }`;
              return;
            }
          }

          progress.report({ message: t(lang, 'backend.stepVerify') });
          const info = await this.probe(
            venvAgtPath(process.platform, this.venvDir),
            'managed',
          );
          if (!info) {
            outcome.failure = 'the installed agt did not answer --version';
            return;
          }
          outcome.info = info;
          // Resolve the just-installed executable *before* starting: the
          // lifecycle may still hold an empty command from before install.
          this.apply(info);
          progress.report({ message: t(lang, 'backend.stepStart') });
          outcome.started = await this.opts.lifecycle.start();
        } catch (err) {
          outcome.failure = err instanceof Error ? err.message : String(err);
        } finally {
          cancellation.dispose();
        }
      },
    );

    if (outcome.info) {
      this.apply(outcome.info);
      this.opts.setStatus('ready');
      const logsLabel = t(lang, 'backend.viewLogs');
      if (outcome.started === false) {
        const choice = await vscode.window.showWarningMessage(
          t(lang, 'backend.installedNoStart', { version: outcome.info.version }),
          logsLabel,
        );
        if (choice === logsLabel) this.opts.channel.show(true);
      } else {
        const choice = await vscode.window.showInformationMessage(
          t(lang, 'backend.success', {
            version: outcome.info.version,
            path: outcome.info.path,
          }),
          logsLabel,
        );
        if (choice === logsLabel) this.opts.channel.show(true);
      }
      await this.opts.onInstalled();
      return;
    }

    this.opts.setStatus('missing');
    if (outcome.cancelled) {
      void vscode.window.showInformationMessage(t(lang, 'backend.cancelled'));
      return;
    }
    if (outcome.noPython) {
      const openLabel = t(lang, 'backend.openPython');
      const logsLabel = t(lang, 'backend.viewLogs');
      const choice = await vscode.window.showErrorMessage(
        t(lang, 'backend.noPython'),
        openLabel,
        logsLabel,
      );
      if (choice === openLabel) void vscode.env.openExternal(vscode.Uri.parse(PYTHON_URL));
      if (choice === logsLabel) this.opts.channel.show(true);
      return;
    }
    const logsLabel = t(lang, 'backend.viewLogs');
    const retryLabel = t(lang, 'backend.retry');
    const choice = await vscode.window.showErrorMessage(
      t(lang, 'backend.failed', { message: outcome.failure ?? 'unknown error' }),
      logsLabel,
      retryLabel,
    );
    if (choice === logsLabel) this.opts.channel.show(true);
    if (choice === retryLabel) void this.requestInstall();
  }

  /** ``uv`` first (it can also fetch Python), then system Python ≥ 3.12. */
  private async findPython(): Promise<PythonRuntime | null> {
    try {
      const uv = await runProcess('uv', ['--version'], {
        timeoutMs: PROBE_TIMEOUT_MS,
      });
      if (uv.code === 0) return { tool: 'uv', cmd: 'uv', prefixArgs: [] };
    } catch {
      // No uv — fall through to system Python.
    }
    for (const candidate of pythonCandidates(process.platform)) {
      try {
        const result = await runProcess(
          candidate.cmd,
          [
            ...candidate.prefixArgs,
            '-c',
            'import sys; print("%d.%d" % sys.version_info[:2])',
          ],
          { timeoutMs: PROBE_TIMEOUT_MS },
        );
        if (result.code !== 0) continue;
        const version = parsePythonVersion(result.stdout);
        if (version && supportsPython312(version)) {
          return { tool: 'python', cmd: candidate.cmd, prefixArgs: candidate.prefixArgs };
        }
      } catch {
        // Try the next launcher.
      }
    }
    return null;
  }

  private log(message: string): void {
    this.opts.channel.appendLine(message);
  }

  private appendResult(result: RunResult): void {
    const output = `${result.stdout}${result.stderr}`.trim();
    if (output) this.opts.channel.appendLine(output);
    this.opts.channel.appendLine(
      `[exit ${result.code ?? 'none'}${result.timedOut ? ', timed out' : ''}]`,
    );
  }
}
