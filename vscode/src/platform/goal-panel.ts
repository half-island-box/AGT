import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { openArtifact } from './artifact-view';
import type { ServerLifecycle } from '../core/lifecycle';
import type {
  GoalRunInfo,
  GoalTreeInfo,
  UiLanguage,
} from '../core/types';

export interface GoalBridgeContext {
  extensionUri: vscode.Uri;
  context: vscode.ExtensionContext;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
  setLanguage(language: UiLanguage): Promise<void>;
  openChat(): void;
  openSop(): void;
  openSopFile(uri: vscode.Uri): void;
  openSettings(): void;
}

interface BridgeRequest {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

const SELECTED_RUN_KEY = 'agt.goal.selectedRun';
const INSPECTOR_WIDTH_KEY = 'agt.goal.inspectorWidth';

export class GoalPanel {
  private view: vscode.WebviewView | undefined;
  private ctx: GoalBridgeContext;
  private selectedRunId: string | null;
  private selectedNodeId: string | null = null;
  private inspectorWidth: number | null = null;

  constructor(ctx: GoalBridgeContext) {
    this.ctx = ctx;
    this.selectedRunId = ctx.context.workspaceState.get<string | null>(SELECTED_RUN_KEY) ?? null;
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

  notifyLanguage(language: UiLanguage): void {
    if (!this.view) return;
    void this.view.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  attach(ctx: GoalBridgeContext): void {
    this.ctx = ctx;
  }

  private async pushRefresh(): Promise<void> {
    await this.view?.webview.postMessage({
      type: 'event',
      event: 'refresh',
      payload: await this.bootPayload(),
    });
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.view?.webview;
    if (!webview) return '<!DOCTYPE html><body></body>';
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'dist', 'webview-goal.js'),
    );
    const cssPath = join(extensionUri.fsPath, 'dist', 'webview-goal.css');
    let css = '';
    try {
      css = readFileSync(cssPath, 'utf8');
    } catch {
      css = '';
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
<body><div id="agt-root"></div><script src="${scriptUri}"></script></body>
</html>`;
  }

  private async bootPayload(): Promise<Record<string, unknown>> {
    const online = await this.ctx.lifecycle.probe();
    let runs: GoalRunInfo[] = [];
    if (online) {
      try {
        runs = (await this.ctx.lifecycle.api.goalRuns()).runs;
      } catch {
        runs = [];
      }
    }
    if (this.selectedRunId && !runs.some((run) => run.root_job_id === this.selectedRunId)) {
      // The tree can still exist briefly after a list page misses it, but an
      // empty server means there is nothing meaningful to restore.
      if (!online) {
        this.selectedRunId = null;
      }
    }
    let tree: GoalTreeInfo | null = null;
    if (online && this.selectedRunId) {
      try {
        tree = await this.ctx.lifecycle.api.jobTree(this.selectedRunId);
      } catch {
        tree = null;
      }
    }
    if (tree) {
      this.selectedRunId = tree.root_job_id;
      await this.ctx.context.workspaceState.update(SELECTED_RUN_KEY, tree.root_job_id);
    }
    let identities: Awaited<ReturnType<typeof this.ctx.lifecycle.api.identities>> = [];
    let daemons: Awaited<ReturnType<typeof this.ctx.lifecycle.api.daemons>> = [];
    if (online) {
      try {
        identities = await this.ctx.lifecycle.api.identities();
      } catch {
        identities = [];
      }
      try {
        daemons = await this.ctx.lifecycle.api.daemons();
      } catch {
        daemons = [];
      }
    }
    return {
      language: this.ctx.language(),
      state: this.ctx.lifecycle.state,
      runs,
      selectedRunId: this.selectedRunId,
      tree,
      selectedNodeId: this.selectedNodeId,
      identities,
      daemons,
      workspaceFolder: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
      inspectorWidth:
        this.inspectorWidth ??
        this.ctx.context.workspaceState.get<number>(INSPECTOR_WIDTH_KEY) ??
        null,
    };
  }

  async handle(msg: BridgeRequest): Promise<void> {
    if (msg?.type !== 'request') return;
    let result: unknown;
    try {
      switch (msg.method) {
        case 'goal.boot':
          result = await this.bootPayload();
          break;

        case 'goal.submit': {
          const online = await this.ctx.lifecycle.ensureStarted();
          if (!online) throw new Error('server-offline');
          const submitted = await this.ctx.lifecycle.api.submitGoal({
            goal: String(msg.params?.goal ?? '').trim(),
            identityDid: msg.params?.identityDid ? String(msg.params.identityDid) : null,
            provider: msg.params?.provider ? String(msg.params.provider) : null,
            daemonId: msg.params?.daemonId ? String(msg.params.daemonId) : null,
            dir: msg.params?.dir ? String(msg.params.dir) : null,
            maxDepth: Number(msg.params?.maxDepth ?? 5),
            maxDescendants: Number(msg.params?.maxDescendants ?? 64),
          });
          this.selectedRunId = submitted.root_job_id;
          this.selectedNodeId = submitted.root_job_id;
          await this.ctx.context.workspaceState.update(SELECTED_RUN_KEY, submitted.root_job_id);
          result = { ...submitted, ...(await this.bootPayload()) };
          break;
        }

        case 'goal.select': {
          this.selectedRunId = String(msg.params?.rootJobId ?? '');
          this.selectedNodeId = this.selectedRunId;
          await this.ctx.context.workspaceState.update(SELECTED_RUN_KEY, this.selectedRunId);
          result = await this.bootPayload();
          break;
        }

        case 'goal.tree': {
          const rootJobId = String(msg.params?.rootJobId ?? this.selectedRunId ?? '');
          result = await this.ctx.lifecycle.api.jobTree(rootJobId);
          break;
        }

        case 'goal.nodeLog': {
          result = await this.ctx.lifecycle.api.jobLog(String(msg.params?.jobId ?? ''));
          break;
        }

        case 'goal.taskStatus': {
          result = await this.ctx.lifecycle.api.taskStatus(String(msg.params?.taskId ?? ''));
          break;
        }

        case 'goal.answer': {
          const jobId = String(msg.params?.jobId ?? '');
          const answer = String(msg.params?.answer ?? '');
          await this.ctx.lifecycle.api.appendChatInput(jobId, answer);
          result = await this.bootPayload();
          break;
        }

        case 'goal.cancel': {
          const rootJobId = String(msg.params?.rootJobId ?? this.selectedRunId ?? '');
          result = await this.ctx.lifecycle.api.cancelJob(rootJobId);
          break;
        }

        case 'goal.export': {
          const rootJobId = String(msg.params?.rootJobId ?? this.selectedRunId ?? '');
          const exported = await this.ctx.lifecycle.api.goalExport(
            rootJobId,
            msg.params?.includeFailed === true,
            msg.params?.keepPlanning === true,
          );
          const target = await vscode.window.showSaveDialog({
            saveLabel: 'Save SOP draft',
            defaultUri: vscode.workspace.workspaceFolders?.[0]
              ? vscode.Uri.joinPath(vscode.workspace.workspaceFolders[0].uri, `${rootJobId}.sop.yaml`)
              : undefined,
            filters: { 'SOP template': ['yaml', 'yml'] },
          });
          if (!target) {
            result = { canceled: true, yaml: exported.yaml, warnings: exported.warnings };
            break;
          }
          await vscode.workspace.fs.writeFile(target, Buffer.from(exported.yaml, 'utf8'));
          result = {
            canceled: false,
            path: target.fsPath,
            warnings: exported.warnings,
          };
          this.ctx.openSopFile(target);
          break;
        }

        case 'openChat':
          this.ctx.openChat();
          result = { ok: true };
          break;

        case 'openSop':
          this.ctx.openSop();
          result = { ok: true };
          break;

        case 'openSettings':
          this.ctx.openSettings();
          result = { ok: true };
          break;

        case 'openArtifact':
          result = await openArtifact(this.ctx.lifecycle, {
            artifactUri: String(msg.params?.artifactUri ?? ''),
            dir: msg.params?.dir ? String(msg.params.dir) : null,
            workspaceFolder: msg.params?.workspaceFolder
              ? String(msg.params.workspaceFolder)
              : null,
          });
          break;

        case 'goal.setInspectorWidth': {
          const width = Number(msg.params?.width);
          if (Number.isFinite(width)) {
            this.inspectorWidth = Math.max(300, Math.min(760, Math.round(width)));
            await this.ctx.context.workspaceState.update(
              INSPECTOR_WIDTH_KEY,
              this.inspectorWidth,
            );
          }
          result = { ok: true, width: this.inspectorWidth };
          break;
        }

        case 'setLanguage': {
          const language = String(msg.params?.language ?? '') as UiLanguage;
          if (language === 'zh' || language === 'en') await this.ctx.setLanguage(language);
          result = await this.bootPayload();
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
        error: String(err instanceof Error ? err.message : err),
      });
    }
  }
}
