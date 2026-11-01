import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { appendFile, mkdir, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';
import type { ServerLifecycle } from '../core/lifecycle';
import { localDaemonId } from '../core/sop/run';
import type {
  DaemonInfo,
  IdentityWithBindings,
  UiLanguage,
} from '../core/types';

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
    const daemons = lifecycle.state === 'online' ? await this.ctx.getDaemons() : [];
    let identities: IdentityWithBindings[] = [];
    if (lifecycle.state === 'online') {
      try {
        identities = await lifecycle.api.identitiesWithBindings();
      } catch {
        identities = [];
      }
    }
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
      daemons,
      identities,
      localDaemonId: localDaemonId(
        daemons.map((daemon) => ({
          daemon_id: daemon.daemon_id,
          url: daemon.url,
        })),
        serverUrlHost(this.config().get<string>('serverUrl') ?? ''),
      ),
      capabilities: { pairing: true },
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
          const daemons = online ? await this.ctx.getDaemons() : [];
          let identities: IdentityWithBindings[] = [];
          if (online) {
            try {
              identities = await this.ctx.lifecycle.api.identitiesWithBindings();
            } catch {
              identities = [];
            }
          }
          result = {
            state: this.ctx.lifecycle.state,
            latencyMs: this.ctx.lifecycle.latencyMs,
            daemons,
            identities,
            localDaemonId: localDaemonId(
              daemons.map((daemon) => ({
                daemon_id: daemon.daemon_id,
                url: daemon.url,
              })),
              serverUrlHost(this.config().get<string>('serverUrl') ?? ''),
            ),
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
        case 'refreshDaemons': {
          // Re-read ~/.agt/daemons on the Server and reconcile its links:
          // a newly registered target starts being dialed, an offline one
          // is re-dialed now instead of after its up-to-60 s backoff.
          // ``daemonId`` (per-row button) narrows the forced re-dial.
          const daemonId = msg.params?.daemonId ? String(msg.params.daemonId) : null;
          let rescan: unknown;
          try {
            rescan = await this.ctx.lifecycle.api.refreshDaemons(daemonId);
          } catch (err) {
            // Older Server without the endpoint, or a timeout: the listing
            // below still runs, and the UI reports what went wrong.
            rescan = { error: err instanceof Error ? err.message : String(err) };
          }
          await this.ctx.lifecycle.probe();
          this.ctx.onConfigOrStateChange();
          result = { ...(await this.bootPayload()), rescan };
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
        case 'pairingState':
          result = await this.ctx.lifecycle.api.pairingState();
          break;
        case 'pairingSecret':
          result = await this.ctx.lifecycle.api.pairingSecret(
            msg.params?.reveal === true,
          );
          break;
        case 'pairingInvite':
          result = await this.ctx.lifecycle.api.pairingInvite(
            String(msg.params?.name ?? ''),
            String(msg.params?.url ?? ''),
            msg.params?.daemonId ? String(msg.params.daemonId) : null,
          );
          break;
        case 'pairingAddDaemon': {
          const p = msg.params ?? {};
          result = await this.ctx.lifecycle.api.pairingAddDaemon({
            name: String(p.name ?? '').trim(),
            url: String(p.url ?? '').trim(),
            capabilities: String(p.capabilities ?? '').trim() || null,
          });
          break;
        }
        case 'pairingRemoveDaemon': {
          const p = msg.params ?? {};
          result = await this.ctx.lifecycle.api.pairingRemoveDaemon({
            name: p.name ? String(p.name) : undefined,
            url: p.url ? String(p.url) : undefined,
            daemonId: p.daemonId ? String(p.daemonId) : undefined,
          });
          break;
        }
        case 'daemonConfigShow': {
          const daemonId = String(msg.params?.daemonId ?? '');
          if (!daemonId) {
            throw new Error('daemon-id-required');
          }
          if (daemonId !== await this.currentLocalDaemonId()) {
            throw new Error('remote-daemon-config-unsupported');
          }
          result = await this.ctx.lifecycle.api.daemonConfig();
          break;
        }
        case 'daemonConfigSet': {
          const p = msg.params ?? {};
          const daemonId = String(p.daemonId ?? '');
          if (!daemonId) {
            throw new Error('daemon-id-required');
          }
          if (daemonId !== await this.currentLocalDaemonId()) {
            throw new Error('remote-daemon-config-unsupported');
          }
          result = await this.ctx.lifecycle.api.setDaemonConfig({
            maxConcurrent: typeof p.maxConcurrent === 'number' ? p.maxConcurrent : undefined,
            agentTimeout: typeof p.agentTimeout === 'number' ? p.agentTimeout : undefined,
            workDir: typeof p.workDir === 'string' ? p.workDir : undefined,
            agentCwdAllowlist: Array.isArray(p.agentCwdAllowlist)
              ? p.agentCwdAllowlist.filter((item): item is string => typeof item === 'string')
              : undefined,
          });
          break;
        }
        case 'identityCreate': {
          const p = msg.params ?? {};
          const name = String(p.name ?? '').trim();
          if (!name) throw new Error('identity-name-required');
          const capabilities = Array.isArray(p.capabilities)
            ? p.capabilities
                .map((value) => String(value).trim())
                .filter(Boolean)
            : [];
          const created = await this.ctx.lifecycle.api.createIdentity({
            daemon_id: (await this.currentLocalDaemonId()) ?? 'local',
            name,
            slug: p.slug ? String(p.slug).trim() : null,
            description: String(p.description ?? '').trim(),
            capabilities: [...new Set(capabilities)],
          });
          result = {
            created,
            identities: await this.ctx.lifecycle.api.identitiesWithBindings(),
          };
          break;
        }
        case 'identityUpdate': {
          const p = msg.params ?? {};
          const did = String(p.did ?? '');
          if (!did) throw new Error('identity-did-required');
          const current = (await this.ctx.lifecycle.api.identitiesWithBindings())
            .find((identity) => identity.did === did);
          if (!current) throw new Error('identity-not-found');
          const capabilities = Array.isArray(p.capabilities)
            ? [...new Set(
                p.capabilities
                  .map((value) => String(value).trim())
                  .filter(Boolean),
              )]
            : current.capabilities;
          const updated = await this.ctx.lifecycle.api.updateIdentity(did, {
            name: String(p.name ?? current.name).trim(),
            description: String(p.description ?? current.description).trim(),
            add_capabilities: capabilities.filter(
              (capability) => !current.capabilities.includes(capability),
            ),
            remove_capabilities: current.capabilities.filter(
              (capability) => !capabilities.includes(capability),
            ),
          });
          result = {
            updated,
            identities: await this.ctx.lifecycle.api.identitiesWithBindings(),
          };
          break;
        }
        case 'identityBindingCreate': {
          const p = msg.params ?? {};
          const did = String(p.did ?? '');
          const daemonId = String(p.daemonId ?? '');
          const provider = String(p.provider ?? '');
          if (!did || !daemonId || !provider) {
            throw new Error('identity-binding-required');
          }
          await this.ctx.lifecycle.api.createIdentityBinding(did, {
            daemonId,
            provider,
            priority: Number.isFinite(Number(p.priority))
              ? Math.trunc(Number(p.priority))
              : 0,
            isDefault: p.isDefault === true,
          });
          result = {
            identities: await this.ctx.lifecycle.api.identitiesWithBindings(),
          };
          break;
        }
        case 'identityBindingUpdate': {
          const p = msg.params ?? {};
          const did = String(p.did ?? '');
          const bindingId = Number(p.bindingId);
          if (!did || !Number.isInteger(bindingId)) {
            throw new Error('identity-binding-required');
          }
          await this.ctx.lifecycle.api.updateIdentityBinding(did, bindingId, {
            priority: p.priority === undefined
              ? undefined
              : Math.trunc(Number(p.priority)),
            isDefault: typeof p.isDefault === 'boolean'
              ? p.isDefault
              : undefined,
          });
          result = {
            identities: await this.ctx.lifecycle.api.identitiesWithBindings(),
          };
          break;
        }
        case 'identityBindingDelete': {
          const p = msg.params ?? {};
          const did = String(p.did ?? '');
          const bindingId = Number(p.bindingId);
          if (!did || !Number.isInteger(bindingId)) {
            throw new Error('identity-binding-required');
          }
          await this.ctx.lifecycle.api.deleteIdentityBinding(did, bindingId);
          result = {
            identities: await this.ctx.lifecycle.api.identitiesWithBindings(),
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

  private async currentLocalDaemonId(): Promise<string | null> {
    if (this.ctx.lifecycle.state !== 'online') {
      return null;
    }
    const daemons = await this.ctx.getDaemons();
    return localDaemonId(
      daemons.map((daemon) => ({
        daemon_id: daemon.daemon_id,
        url: daemon.url,
      })),
      serverUrlHost(this.config().get<string>('serverUrl') ?? ''),
    );
  }
}

function shellQuote(value: string): string {
  return `'${value.replaceAll("'", `'\\''`)}'`;
}

function serverUrlHost(url: string): string {
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}
