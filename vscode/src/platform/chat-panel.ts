import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { ServerLifecycle } from '../core/lifecycle';
import type {
  ChatSessionInfo,
  SessionHistoryInfo,
  TaskStatusInfo,
  TaskStreamEvent,
  UiLanguage,
} from '../core/types';

export interface ChatBridgeContext {
  extensionUri: vscode.Uri;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
  setLanguage(language: UiLanguage): Promise<void>;
  openSettings(): void;
  openSop(): void;
  openGoal(): void;
}

interface BridgeRequest {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

export class ChatPanel {
  private static current: ChatPanel | undefined;

  static show(ctx: ChatBridgeContext): void {
    if (ChatPanel.current) {
      ChatPanel.current.attach(ctx);
      ChatPanel.current.panel.reveal();
      void ChatPanel.current.pushRefresh();
      return;
    }
    ChatPanel.current = new ChatPanel(ctx);
  }

  static notifyLanguage(language: UiLanguage): void {
    void ChatPanel.current?.panel.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  private readonly panel: vscode.WebviewPanel;
  private ctx: ChatBridgeContext;
  private streamAbort: AbortController | undefined;
  private streaming = false;

  private constructor(ctx: ChatBridgeContext) {
    this.ctx = ctx;
    this.panel = vscode.window.createWebviewPanel(
      'agt.chat',
      'AGT Chat',
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(ctx.extensionUri, 'dist')],
      },
    );
    this.panel.webview.html = this.buildHtml(ctx.extensionUri);
    this.panel.webview.onDidReceiveMessage((msg) => void this.handle(msg as BridgeRequest));
    this.panel.onDidDispose(() => {
      this.streamAbort?.abort();
      if (ChatPanel.current === this) {
        ChatPanel.current = undefined;
      }
    });
  }

  attach(ctx: ChatBridgeContext): void {
    this.ctx = ctx;
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.panel.webview;
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'dist', 'webview-chat.js'),
    );
    const cssPath = join(extensionUri.fsPath, 'dist', 'webview-chat.css');
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
    ].join('; ');
    return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta http-equiv="Content-Security-Policy" content="${csp}">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>${css}</style>
</head>
<body class="agd-chat-body">
<div id="agt-root"></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
  }

  private async pushRefresh(): Promise<void> {
    await this.panel.webview.postMessage({
      type: 'event',
      event: 'refresh',
      payload: await this.bootPayload(),
    });
  }

  private async bootPayload(): Promise<Record<string, unknown>> {
    await this.ctx.lifecycle.probe();
    if (this.ctx.lifecycle.state !== 'online') {
      return {
        language: this.ctx.language(),
        state: this.ctx.lifecycle.state,
        daemons: [],
        runtimes: [],
        identities: [],
        sessions: [],
        workspaceFolders: [],
      };
    }
    const api = this.ctx.lifecycle.api;
    const [daemons, runtimes, identities] = await Promise.all([
      api.daemons(),
      api.runtimes(),
      api.identitiesWithBindings(),
    ]);
    return {
      language: this.ctx.language(),
      state: this.ctx.lifecycle.state,
      daemons,
      runtimes,
      identities,
      sessions: [],
      workspaceFolders: vscode.workspace.workspaceFolders?.map((folder) => folder.uri.fsPath) ?? [],
    };
  }

  private async emitChat(payload: Record<string, unknown>): Promise<void> {
    await this.panel.webview.postMessage({
      type: 'event',
      event: 'chat',
      payload,
    });
  }

  private async handle(msg: BridgeRequest): Promise<void> {
    if (!msg || msg.type !== 'request') return;
    try {
      let result: unknown;
      switch (msg.method) {
        case 'boot':
          result = await this.bootPayload();
          break;
        case 'refresh':
          result = await this.bootPayload();
          break;
        case 'setLanguage': {
          const language = msg.params?.language;
          if (language === 'zh' || language === 'en') {
            await this.ctx.setLanguage(language);
          }
          result = { ok: true };
          break;
        }
        case 'openSettings':
          this.ctx.openSettings();
          result = { ok: true };
          break;
        case 'openSop':
          this.ctx.openSop();
          result = { ok: true };
          break;

        case 'openGoal':
          this.ctx.openGoal();
          result = { ok: true };
          break;
        case 'sessions': {
          const daemonId = String(msg.params?.daemonId ?? '');
          result = { sessions: await this.ctx.lifecycle.api.daemonSessions(daemonId) };
          break;
        }
        case 'selectSession': {
          const sessionId = String(msg.params?.sessionId ?? '');
          const resolved = await this.ctx.lifecycle.api.resolveSession(sessionId);
          const [job, history] = await Promise.all([
            this.ctx.lifecycle.api.jobStatus(resolved.job_id),
            this.ctx.lifecycle.api.sessionHistory(sessionId).catch((err: unknown) => err),
          ]);
          result = {
            resolved,
            jobStatus: job,
            history: history instanceof Error ? undefined : (history as SessionHistoryInfo),
            historyError:
              history instanceof Error ? cleanError(history) : undefined,
          };
          break;
        }
        case 'send': {
          if (this.streaming) {
            throw new Error('chat already streaming');
          }
          const params = msg.params ?? {};
          void this.runTurn({
            prompt: String(params.prompt ?? ''),
            daemonId: String(params.daemonId ?? ''),
            provider: String(params.provider ?? ''),
            identityDid: params.identityDid ? String(params.identityDid) : null,
            agentCwd: params.agentCwd ? String(params.agentCwd) : null,
            currentJobId: params.currentJobId ? String(params.currentJobId) : null,
          });
          result = { accepted: true };
          break;
        }
        case 'cancel': {
          const jobId = String(msg.params?.jobId ?? '');
          if (!jobId) throw new Error('no active chat job');
          await this.ctx.lifecycle.api.cancelJob(jobId);
          await this.emitChat({ kind: 'cancelling', jobId });
          result = { cancelled: true };
          break;
        }
        default:
          throw new Error(`unknown bridge method: ${msg.method}`);
      }
      await this.panel.webview.postMessage({ type: 'response', id: msg.id, result });
    } catch (err) {
      await this.panel.webview.postMessage({
        type: 'response',
        id: msg.id,
        error: cleanError(err),
      });
    }
  }

  private async runTurn(input: {
    prompt: string;
    daemonId: string;
    provider: string;
    identityDid: string | null;
    agentCwd: string | null;
    currentJobId: string | null;
  }): Promise<void> {
    this.streaming = true;
    this.streamAbort = new AbortController();
    const api = this.ctx.lifecycle.api;
    try {
      const job = input.currentJobId
        ? await api.appendChatInput(input.currentJobId, input.prompt)
        : await api.createChatJob({
            prompt: input.prompt,
            provider: input.provider,
            targetDaemonId: input.daemonId,
            identityDid: input.identityDid,
            agentCwd: input.agentCwd,
          });
      await this.emitChat({ kind: 'started', ...job });

      let finalEvent: TaskStreamEvent | undefined;
      try {
        for await (const event of api.streamTask(job.task_id, this.streamAbort.signal)) {
          await this.emitChat({ kind: event.type, ...event });
          if (event.type === 'done') {
            finalEvent = event;
            break;
          }
        }
      } catch (err) {
        if (this.streamAbort.signal.aborted) {
          return;
        }
        await this.emitChat({ kind: 'streamFallback', message: cleanError(err) });
      }

      if (!finalEvent) {
        const status = await this.pollTask(job.task_id);
        finalEvent = {
          type: 'done',
          status: status.status,
          stdout: status.stdout ?? '',
          stderr: status.stderr ?? '',
          session_id: status.session_id,
          exit_code: status.exit_code,
        };
        await this.emitChat({ kind: 'done', ...finalEvent });
      }

      const [jobStatus, sessions] = await Promise.all([
        api.jobStatus(job.job_id).catch((err: unknown) => err),
        api.daemonSessions(input.daemonId).catch((err: unknown) => err),
      ]);
      await this.emitChat({
        kind: 'settled',
        jobStatus: jobStatus instanceof Error ? undefined : jobStatus,
        sessions: sessions instanceof Error ? [] : (sessions as ChatSessionInfo[]),
        sessionsError: sessions instanceof Error ? cleanError(sessions) : undefined,
      });
    } catch (err) {
      await this.emitChat({ kind: 'error', message: cleanError(err) });
    } finally {
      this.streaming = false;
      this.streamAbort = undefined;
    }
  }

  private async pollTask(taskId: string): Promise<TaskStatusInfo> {
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      if (this.streamAbort?.signal.aborted) {
        throw new Error('chat stream aborted');
      }
      const status = await this.ctx.lifecycle.api.taskStatus(taskId);
      if (status.status !== 'pending' && status.status !== 'running') {
        return status;
      }
      await sleep(1000);
    }
    throw new Error('chat task did not finish within 300s');
  }
}

function cleanError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return message.replace(/^Error:\s*/, '');
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
