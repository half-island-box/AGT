import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { appendFile, mkdir, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import type { ServerLifecycle } from '../core/lifecycle';
import type { DaemonInfo, UiLanguage } from '../core/types';

export interface BridgeContext {
  extensionUri: vscode.Uri;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
  getDaemons(): Promise<DaemonInfo[]>;
  onConfigOrStateChange(): void;
}

interface BridgeRequest {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

export class SettingsPanel {
  private view: vscode.WebviewView | undefined;
  private ctx: BridgeContext;

  constructor(ctx: BridgeContext) {
    this.ctx = ctx;
  }

  async present(view: vscode.WebviewView): Promise<void> {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.ctx.extensionUri, 'dist')],
    };
    view.webview.html = this.buildHtml(this.ctx.extensionUri);
    await this.pushRefresh();
  }

  async deactivate(): Promise<void> {
    this.view = undefined;
  }

  async dispatch(msg: unknown): Promise<void> {
    await this.handle(msg as BridgeRequest);
  }

  notifyActive(ctx: BridgeContext): void {
    this.attach(ctx);
    void this.pushRefresh();
  }

  attach(ctx: BridgeContext): void {
    this.ctx = ctx;
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.view?.webview;
    if (!webview) return '<!DOCTYPE html><body></body>';
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'dist', 'webview-settings.js'),
    );
    const cssPath = join(extensionUri.fsPath, 'dist', 'webview-settings.css');
    let css = '';
    try {
      css = readFileSync(cssPath, 'utf8');
    } catch {
      // CSS is optional when only the JS bundle exists during early dev.
    }
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src ${webview.cspSource}`,
      `font-src ${webview.cspSource}`,
    ].join('; ');
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>${css}</style>
</head>
<body>
<div id="agt-root"></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
  }

  private config(): vscode.WorkspaceConfiguration {
    return vscode.workspace.getConfiguration('agt');
  }

  private async bootPayload(): Promise<Record<string, unknown>> {
    const cfg = this.config();
    const lifecycle = this.ctx.lifecycle;
    return {
      serverUrl: cfg.get<string>('serverUrl'),
      autoStart: cfg.get<boolean>('autoStart'),
      startCommand: cfg.get<string>('startCommand'),
      stopCommand: cfg.get<string>('stopCommand'),
      autoStartDaemon: cfg.get<boolean>('autoStartDaemon'),
      daemonStartCommand: cfg.get<string>('daemonStartCommand'),
      daemonStopCommand: cfg.get<string>('daemonStopCommand'),
      daemonRunning: lifecycle.daemonRunning(),
      language: this.ctx.language(),
      state: lifecycle.state,
      latencyMs: lifecycle.latencyMs,
      daemons: lifecycle.state === 'online' ? await this.ctx.getDaemons() : [],
    };
  }

  private async pushRefresh(): Promise<void> {
    await this.view?.webview.postMessage({
      type: 'event',
      event: 'refresh',
      payload: await this.bootPayload(),
    });
  }

  async handle(msg: BridgeRequest): Promise<void> {
    if (!msg || msg.type !== 'request') {
      return;
    }
    try {
      let result: unknown;
      switch (msg.method) {
        case 'boot':
          result = await this.bootPayload();
          break;
        case 'testConnection': {
          const online = await this.ctx.lifecycle.probe();
          this.ctx.onConfigOrStateChange();
          result = {
            state: this.ctx.lifecycle.state,
            latencyMs: this.ctx.lifecycle.latencyMs,
            daemons: online ? await this.ctx.getDaemons() : [],
          };
          break;
        }
        case 'setConfig': {
          const params = msg.params ?? {};
          const allowed = [
            'serverUrl',
            'autoStart',
            'startCommand',
            'stopCommand',
            'autoStartDaemon',
            'daemonStartCommand',
            'daemonStopCommand',
            'language',
          ];
          for (const [key, value] of Object.entries(params)) {
            if (allowed.includes(key)) {
              await this.config().update(key, value, vscode.ConfigurationTarget.Global);
            }
          }
          this.ctx.onConfigOrStateChange();
          result = { ok: true, ...(await this.bootPayload()) };
          break;
        }
        case 'startServer': {
          await this.ctx.lifecycle.ensureStarted();
          // Same contract as window startup: a server this client started
          // comes up together with the local daemon (switch permitting).
          await this.ctx.lifecycle.ensureDaemonStarted();
          this.ctx.onConfigOrStateChange();
          result = await this.bootPayload();
          break;
        }
        case 'startDaemon': {
          await this.ctx.lifecycle.startDaemon();
          this.ctx.onConfigOrStateChange();
          result = await this.bootPayload();
          break;
        }
        case 'stopDaemon': {
          await this.ctx.lifecycle.stopDaemon();
          this.ctx.onConfigOrStateChange();
          result = await this.bootPayload();
          break;
        }
        case 'stopServer': {
          await this.ctx.lifecycle.stop();
          this.ctx.onConfigOrStateChange();
          result = await this.bootPayload();
          break;
        }
        case 'refresh': {
          await this.ctx.lifecycle.probe();
          result = await this.bootPayload();
          break;
        }
        case 'addDaemon': {
          const p = msg.params ?? {};
          const name = String(p.name ?? '').trim();
          const url = String(p.url ?? '').trim();
          const capsRaw = String(p.capabilities ?? '').trim();
          if (!name) {
            throw new Error('name-required');
          }
          if (!/^[A-Za-z0-9_.-]+$/.test(name)) {
            throw new Error('bad-name');
          }
          if (!/^wss?:\/\/\S+$/.test(url)) {
            throw new Error('bad-url');
          }
          // The server reads this file once at startup (reverse-dial
          // targets + static capability overlay). No backend write API
          // exists yet, so the extension writes it directly — the two
          // normally share the same machine.
          const daemonsPath = join(homedir(), '.agt', 'daemons');
          await mkdir(dirname(daemonsPath), { recursive: true });
          let existing = '';
          try {
            existing = await readFile(daemonsPath, 'utf8');
          } catch {
            // First entry — file does not exist yet.
          }
          const urlExists = existing
            .split(/\r?\n/)
            .some((line) => line.trim().split(/\s+/).includes(url));
          if (urlExists) {
            throw new Error('duplicate-url');
          }
          const caps = capsRaw
            .split(/[，,]/)
            .map((c) => c.trim())
            .filter(Boolean)
            .join(',');
          const entry = caps ? `${name} ${url} ${caps}` : `${name} ${url}`;
          const prefix = existing && !existing.endsWith('\n') ? '\n' : '';
          await appendFile(daemonsPath, `${prefix}${entry}\n`, 'utf8');
          const serverUrl = this.config().get<string>('serverUrl') ?? 'http://127.0.0.1:8420';
          const shellEntry = shellQuote(entry);
          const shellServerUrl = shellQuote(serverUrl);
          result = {
            ok: true,
            daemonsPath,
            command: [
              'mkdir -p ~/.agt',
              `grep -qxF ${shellEntry} ~/.agt/daemons 2>/dev/null || printf '%s\\n' ${shellEntry} >> ~/.agt/daemons`,
              `AGT_LISTEN_MODE=1 AGT_SERVER_URL=${shellServerUrl} agt daemon start --detach`,
            ].join('\n'),
          };
          break;
        }
        default:
          throw new Error(`unknown bridge method: ${msg.method}`);
      }
      await this.view?.webview.postMessage({ type: 'response', id: msg.id, result });
    } catch (err) {
      await this.view?.webview.postMessage({
        type: 'response',
        id: msg.id,
        error: String(err),
      });
    }
  }
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}
