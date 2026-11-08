import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { ServerLifecycle } from '../core/lifecycle';
import type { AgtApi } from '../core/api';
import { listDirectories } from './directories';
import type {
  ChatInputMessage,
  ChatSessionInfo,
  RunInfo,
  SessionHistoryInfo,
  SessionResolveInfo,
  TaskStatusInfo,
  TaskStreamEvent,
  UiLanguage,
} from '../core/types';

export interface ChatSelection {
  daemonId: string;
  provider?: string;
}

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

/**
 * Prefer the folder containing the active editor, then the first workspace
 * folder. This keeps the chat working directory aligned with the directory the
 * user currently has open in VS Code.
 */
function activeWorkspaceRoot(): string {
  const activeUri = vscode.window.activeTextEditor?.document.uri;
  const activeFolder = activeUri ? vscode.workspace.getWorkspaceFolder(activeUri) : undefined;
  return (
    activeFolder?.uri.fsPath ?? vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? ''
  );
}

function toChatSessionInfo(run: RunInfo): ChatSessionInfo {
  return {
    session_id: run.root_job_id,
    task_id: run.root_job_id,
    job_id: run.root_job_id,
    provider: run.provider,
    identity_did: run.identity_did,
    daemon_id: run.daemon_id,
    dir: run.cwd,
    first_prompt: run.title ?? run.root_job_id,
    last_seen_at: run.updated_at,
  };
}

/**
 * Sessions of the open workspace on one daemon.
 *
 * ``cwd`` is the run-history key, not where the run executed: every chat
 * Job this panel creates records the workspace root, even when a remote
 * daemon runs it, so remote turns group with the workspace's own history.
 */
async function chatSessions(
  api: AgtApi,
  daemonId: string,
): Promise<ChatSessionInfo[]> {
  const list = await api.listRuns({
    mode: 'chat',
    cwd: activeWorkspaceRoot(),
    daemonId,
  });
  return list.runs.map(toChatSessionInfo);
}

async function resolveChatRun(
  api: AgtApi,
  jobId: string,
  dirHint: string | null,
): Promise<SessionResolveInfo> {
  const tree = await api.jobTree(jobId);
  const node = tree.node;
  if (!node.session_id) {
    throw new Error('chat run has no resumed session yet');
  }
  return {
    job_id: jobId,
    task_id: node.current_task_id ?? jobId,
    session_id: node.session_id,
    provider: node.provider,
    identity_did: node.identity_did ?? null,
    daemon_id: node.daemon_id ?? null,
    dir: dirHint,
  };
}

export class ChatPanel {
  private view: vscode.WebviewView | undefined;
  private ctx: ChatBridgeContext;
  private streamAbort: AbortController | undefined;
  private streaming = false;
  private turnGeneration = 0;
  private preferredSelection: ChatSelection | undefined;

  constructor(ctx: ChatBridgeContext) {
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
    this.turnGeneration += 1;
    this.streamAbort?.abort();
    this.streaming = false;
    this.view = undefined;
  }

  async dispatch(msg: unknown): Promise<void> {
    await this.handle(msg as BridgeRequest);
  }

  selectTarget(selection: ChatSelection): void {
    // A running turn is pinned to its original daemon/provider; avoid changing
    // the visible selection underneath an active stream.
    if (this.streaming) return;
    this.preferredSelection = selection;
    if (!this.view) return;
    void this.view.webview.postMessage({
      type: 'event',
      event: 'selection',
      payload: selection,
    });
  }

  notifyWorkspaceRoot(): void {
    if (!this.view) return;
    void this.view.webview.postMessage({
      type: 'event',
      event: 'workspaceRoot',
      payload: activeWorkspaceRoot(),
    });
  }

  notifyLanguage(language: UiLanguage): void {
    if (!this.view) return;
    void this.view.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  attach(ctx: ChatBridgeContext): void {
    this.ctx = ctx;
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.view?.webview;
    if (!webview) return '<!DOCTYPE html><body></body>';
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
<body class="agd-chat-body">
<div id="agt-root"></div>
<script src="${scriptUri}"></script>
</body>
</html>`;
  }

  private async pushRefresh(): Promise<void> {
    await this.view?.webview.postMessage({
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
        workspaceRoot: activeWorkspaceRoot(),
        selection: this.preferredSelection,
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
      workspaceRoot: activeWorkspaceRoot(),
      selection: this.preferredSelection,
    };
  }

  private async emitChat(payload: Record<string, unknown>): Promise<void> {
    await this.view?.webview.postMessage({
      type: 'event',
      event: 'chat',
      payload,
    });
  }

  async handle(msg: BridgeRequest): Promise<void> {
    if (!msg || msg.type !== 'request') return;
    try {
      let result: unknown;
      switch (msg.method) {
        case 'boot':
          result = await this.bootPayload();
          this.preferredSelection = undefined;
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

        case 'openExternal': {
          const url = String(msg.params?.url ?? '');
          const parsed = vscode.Uri.parse(url);
          if (!['http:', 'https:', 'mailto:'].includes(parsed.scheme)) {
            throw new Error('unsupported external link');
          }
          await vscode.env.openExternal(parsed);
          result = { ok: true };
          break;
        }

        case 'sessions': {
          const daemonId = String(msg.params?.daemonId ?? '');
          result = { sessions: await chatSessions(this.ctx.lifecycle.api, daemonId) };
          break;
        }
        case 'selectSession': {
          const jobId = msg.params?.jobId ? String(msg.params.jobId) : null;
          const sessionId = String(msg.params?.sessionId ?? '');
          const dirHint = msg.params?.dir ? String(msg.params.dir) : null;
          const resolved = jobId
            ? await resolveChatRun(this.ctx.lifecycle.api, jobId, dirHint)
            : await this.ctx.lifecycle.api.resolveSession(sessionId);
          // A row's ``dir`` is the run-history key (the workspace root), not
          // the directory the run used — a remote resume would send the local
          // path to a daemon that cannot see it. The Server knows the real one.
          if (jobId) {
            const session = await this.ctx.lifecycle.api
              .resolveSession(resolved.session_id)
              .catch(() => null);
            resolved.dir = session?.dir ?? resolved.dir;
          }
          const [job, history] = await Promise.all([
            this.ctx.lifecycle.api.jobStatus(resolved.job_id),
            this.ctx.lifecycle.api
              .sessionHistory(resolved.session_id)
              .catch((err: unknown) => err),
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
            message: (params.message as ChatInputMessage | undefined) ?? null,
          });
          result = { accepted: true };
          break;
        }
        case 'upload': {
          const params = msg.params ?? {};
          const contentB64 = String(params.contentB64 ?? '');
          if (!contentB64) throw new Error('upload content is required');
          result = await this.ctx.lifecycle.api.uploadInput({
            name: String(params.name ?? ''),
            contentB64,
            mimeType: params.mimeType ? String(params.mimeType) : null,
            source: 'vscode',
          });
          break;
        }
        case 'deleteUpload': {
          const uploadId = String(msg.params?.uploadId ?? '');
          if (uploadId) {
            await this.ctx.lifecycle.api.deleteInputUpload(uploadId).catch(() => {});
          }
          result = { ok: true };
          break;
        }
        case 'stop': {
          const jobId = String(msg.params?.jobId ?? '');
          if (!jobId) throw new Error('no active chat job');
          const stopped = await this.ctx.lifecycle.api.stopChatJob(jobId);
          if (stopped.status === 'waiting_input') {
            // Retire the old turn before emitting: its in-flight SSE reader may
            // still throw asynchronously, but all of its events are now stale.
            this.turnGeneration += 1;
            this.streaming = false;
            const oldStream = this.streamAbort;
            this.streamAbort = undefined;
            oldStream?.abort();
            await this.emitChat({ kind: 'stopped', jobId, status: stopped.status });
          } else {
            await this.emitChat({ kind: 'cancelling', jobId, status: stopped.status });
          }
          result = { stopped: true, status: stopped.status };
          break;
        }
        case 'deleteSession': {
          const jobId = msg.params?.jobId ? String(msg.params.jobId) : null;
          const sessionId = String(msg.params?.sessionId ?? '');
          let provider = String(msg.params?.provider ?? '');
          const daemonId = String(msg.params?.daemonId ?? '');
          let actualSessionId = sessionId;
          if (jobId) {
            const resolved = await resolveChatRun(this.ctx.lifecycle.api, jobId, null);
            actualSessionId = resolved.session_id;
            provider = resolved.provider;
          }
          if (!actualSessionId || !provider || !daemonId) {
            throw new Error('session id, provider, and daemon id are required');
          }
          const deleted = await this.ctx.lifecycle.api.deleteSession(actualSessionId, {
            provider,
            daemonId,
            finalizeActive: true,
          });
          const sessions = await chatSessions(this.ctx.lifecycle.api, daemonId);
          result = { deleted, sessions };
          break;
        }
        case 'projects.directories': {
          const daemonId = msg.params?.daemonId ? String(msg.params.daemonId) : null;
          const path = msg.params?.path ? String(msg.params.path) : null;
          result = await listDirectories(this.ctx.lifecycle.api, daemonId, path);
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
    message?: ChatInputMessage | null;
  }): Promise<void> {
    const generation = ++this.turnGeneration;
    const streamAbort = new AbortController();
    this.streaming = true;
    this.streamAbort = streamAbort;
    const api = this.ctx.lifecycle.api;
    const emitIfCurrent = async (payload: Record<string, unknown>): Promise<void> => {
      if (generation !== this.turnGeneration) return;
      await this.emitChat(payload);
    };
    try {
      const job = input.currentJobId
        ? await api.appendChatInput(input.currentJobId, input.prompt, input.message)
        : await api.createChatJob({
            prompt: input.prompt,
            provider: input.provider,
            targetDaemonId: input.daemonId,
            identityDid: input.identityDid,
            agentCwd: input.agentCwd,
            // History key = the workspace, so a remote turn shows up next to
            // local ones instead of under the remote directory it ran in.
            cwd: activeWorkspaceRoot() || null,
            message: input.message,
          });
      await emitIfCurrent({ kind: 'started', ...job });

      let finalEvent: TaskStreamEvent | undefined;
      try {
        for await (const event of api.streamTask(job.task_id, streamAbort.signal)) {
          if (event.type === 'done') {
            // stderr belongs in execution logs, not the chat transcript.
            const { stderr: _stderr, ...safeDone } = event;
            finalEvent = event;
            await emitIfCurrent({ kind: 'done', ...safeDone });
            break;
          }
          await emitIfCurrent({ kind: event.type, ...event });
        }
      } catch (err) {
        if (streamAbort.signal.aborted) {
          return;
        }
        await emitIfCurrent({ kind: 'streamFallback', message: cleanError(err) });
      }

      if (!finalEvent) {
        const status = await this.pollTask(job.task_id, streamAbort.signal);
        finalEvent = {
          type: 'done',
          status: status.status,
          stdout: status.stdout ?? '',
          session_id: status.session_id,
          exit_code: status.exit_code,
          // Status carries the same summary the SSE ``done`` frame would.
          usage: status.usage ?? null,
          model: status.usage?.model ?? null,
        };
        await emitIfCurrent({ kind: 'done', ...finalEvent });
      }

      const [jobStatus, sessions] = await Promise.all([
        api.jobStatus(job.job_id).catch((err: unknown) => err),
        chatSessions(api, input.daemonId).catch((err: unknown) => err),
      ]);
      await emitIfCurrent({
        kind: 'settled',
        jobStatus: jobStatus instanceof Error ? undefined : jobStatus,
        sessions: sessions instanceof Error ? [] : (sessions as ChatSessionInfo[]),
        sessionsError: sessions instanceof Error ? cleanError(sessions) : undefined,
      });
    } catch (err) {
      await emitIfCurrent({ kind: 'error', message: cleanError(err) });
    } finally {
      if (generation === this.turnGeneration) {
        this.streaming = false;
        this.streamAbort = undefined;
      }
    }
  }

  private async pollTask(taskId: string, signal?: AbortSignal): Promise<TaskStatusInfo> {
    const deadline = Date.now() + 300_000;
    while (Date.now() < deadline) {
      if (signal?.aborted) {
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
