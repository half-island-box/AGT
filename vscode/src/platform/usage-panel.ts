import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { agentOptionsFromDaemons } from '../core/daemons';
import type { ServerLifecycle } from '../core/lifecycle';
import type { UiLanguage } from '../core/types';

export interface UsageBridgeContext {
  extensionUri: vscode.Uri;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
}

interface BridgeRequest {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

/**
 * Token/cost usage for the activity bar, below the Daemons tree.
 *
 * It shows the same totals and daemon × agent × model breakdown as the
 * console dashboard (no trend chart) and answers two bridge methods:
 * ``usage.boot`` (language, server state, agent filter choices, daemon names)
 * and ``usage.summary`` (the aggregation itself).
 */
export class UsagePanel implements vscode.WebviewViewProvider, vscode.Disposable {
  private view: vscode.WebviewView | undefined;

  constructor(private readonly ctx: UsageBridgeContext) {}

  async resolveWebviewView(view: vscode.WebviewView): Promise<void> {
    this.view = view;
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.ctx.extensionUri, 'dist')],
    };
    view.webview.html = this.buildHtml();
    view.webview.onDidReceiveMessage((msg) => void this.handle(msg));
    // Usage changes while the panel is collapsed; refetch whenever it comes
    // back into view instead of polling from the extension side.
    view.onDidChangeVisibility(() => {
      if (view.visible) this.reload();
    });
    view.onDidDispose(() => {
      this.view = undefined;
    });
  }

  dispose(): void {
    this.view = undefined;
  }

  /** Ask the webview to refetch boot data and usage (filter choices may have changed). */
  reload(): void {
    void this.view?.webview.postMessage({ type: 'event', event: 'refresh' });
  }

  notifyLanguage(language: UiLanguage): void {
    void this.view?.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  private buildHtml(): string {
    const webview = this.view?.webview;
    if (!webview) return '<!DOCTYPE html><body></body>';
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(this.ctx.extensionUri, 'dist', 'webview-usage.js'),
    );
    const cssPath = join(this.ctx.extensionUri.fsPath, 'dist', 'webview-usage.css');
    let css = '';
    try {
      css = readFileSync(cssPath, 'utf8');
    } catch {
      // CSS is optional until the first webview build has run.
    }
    const csp = [
      "default-src 'none'",
      `style-src ${webview.cspSource} 'unsafe-inline'`,
      `script-src ${webview.cspSource}`,
      `font-src ${webview.cspSource}`,
      "img-src https: data:",
    ].join('; ');
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>${css}</style>
</head>
<body class="agd-usage-body">
<div id="agt-root"></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
  }

  private async bootPayload(): Promise<Record<string, unknown>> {
    await this.ctx.lifecycle.probe();
    const state = this.ctx.lifecycle.state;
    if (state !== 'online') {
      return {
        language: this.ctx.language(),
        state,
        agents: [],
        daemonNames: {},
      };
    }
    const daemons = await this.ctx.lifecycle.api.daemons().catch(() => []);
    return {
      language: this.ctx.language(),
      state,
      agents: agentOptionsFromDaemons(daemons),
      // Usage history refers to daemons by id; resolve it to the same
      // human-readable name the rest of the extension shows.
      daemonNames: Object.fromEntries(
        daemons.map((daemon) => [
          daemon.daemon_id,
          daemon.daemon_name || daemon.device_name || daemon.daemon_id,
        ]),
      ),
    };
  }

  private async handle(msg: unknown): Promise<void> {
    const request = msg as BridgeRequest | undefined;
    if (!request || request.type !== 'request') return;
    try {
      let result: unknown;
      switch (request.method) {
        case 'usage.boot':
          result = await this.bootPayload();
          break;
        case 'usage.summary': {
          const params = request.params ?? {};
          result = await this.ctx.lifecycle.api.usageSummary({
            agent: params.agent ? String(params.agent) : null,
            from: String(params.from ?? ''),
            to: String(params.to ?? ''),
            bucket: (params.bucket as 'hour' | 'day' | 'month') || 'day',
          });
          break;
        }
        default:
          throw new Error(`unknown bridge method: ${request.method}`);
      }
      await this.view?.webview.postMessage({
        type: 'response',
        id: request.id,
        result,
      });
    } catch (err) {
      await this.view?.webview.postMessage({
        type: 'response',
        id: request.id,
        error: cleanError(err),
      });
    }
  }
}

function cleanError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return message.replace(/^Error:\s*/, '');
}
