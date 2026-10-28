import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { homedir, hostname } from 'node:os';
import { basename, join } from 'node:path';
import { openArtifact } from './artifact-view';
import { isMissingEndpoint } from '../core/api';
import type { ServerLifecycle } from '../core/lifecycle';
import { looksLikeSop, minimalSpan } from '../core/sop/document';
import type { SopPoint } from '../core/sop/layout';
import type { EditIntent } from '../core/sop/model';
import { fingerprint, localDaemonId } from '../core/sop/run';
import {
  applyIntent,
  parseSopText,
  serializeSop,
  type ParsedSop,
} from '../core/sop/yaml-doc';
import type {
  IdentityBindingInfo,
  IdentityWithBindings,
  RunInfo,
  UiLanguage,
} from '../core/types';

export interface SopBridgeContext {
  extensionUri: vscode.Uri;
  context: vscode.ExtensionContext;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
  setLanguage(language: UiLanguage): Promise<void>;
  openChat(): void;
  openGoal(): void;
  openSettings(): void;
}

interface BridgeRequest {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

/** One daemon choice for the inspector's daemon/provider selects. */
export interface SopDaemonOption {
  /** ``daemon_id`` — the value the DSL's ``daemon`` field expects. */
  id: string;
  label: string;
  online: boolean;
  providers: string[];
  url: string | null;
}

export interface SopIdentityOption {
  did: string;
  name: string;
  status: string;
  bindings: Pick<
    IdentityBindingInfo,
    'daemon_id' | 'provider' | 'priority' | 'is_default'
  >[];
}

interface RunState {
  rootJobId: string;
  /** Fingerprint of the submitted document text. */
  specHash: string;
  nodes: Record<string, string>;
  startedAt: number;
}

interface RunSummary {
  rootJobId: string;
  name: string;
  status: string;
  provider: string;
  daemonId: string | null;
  updatedAt: string;
  startedAt: number;
  stale: boolean;
  templatePath?: string | null;
  templateHash?: string | null;
}

const INSPECTOR_WIDTH_KEY = 'agt.sop.inspectorWidth';
const SIDEBAR_WIDTH_KEY = 'agt.sop.sidebarWidth';

const STARTER_YAML = `# SOP template — edit here or on the canvas; both stay in sync.
name: new-pipeline
identities:
  worker: {provider: claude}
tasks:
  - name: first
    identity: worker
    input: 描述这一步要做什么
    output: first-result
`;

/**
 * The visual SOP editor.
 *
 * Holds one YAML document at a time and is the only place that touches the
 * `yaml` package: the webview receives a plain model and sends edit
 * intents, which are applied to the document AST and written through a
 * ``WorkspaceEdit`` — so the dirty marker and Ctrl+Z behave like any other
 * editor in VS Code.
 */
export class SopEditorPanel {
  private view: vscode.WebviewView | undefined;
  private ctx: SopBridgeContext;
  private document: vscode.TextDocument | null = null;
  private parsed: ParsedSop | null = null;
  private disposables: vscode.Disposable[] = [];
  private documentListenersBound = false;
  private templates: { path: string; label: string; nodeCount: number }[] = [];
  private daemonCache: { at: number; list: SopDaemonOption[] } = { at: 0, list: [] };
  private identityCache: { at: number; list: SopIdentityOption[] } = { at: 0, list: [] };
  private runs: RunState[] = [];
  private trackedRunId: string | null = null;
  private selectedRunId: string | null = null;
  private inspectorWidth: number | null = null;
  private sidebarWidth: number | null = null;

  constructor(ctx: SopBridgeContext) {
    this.ctx = ctx;
  }

  async present(view: vscode.WebviewView): Promise<void> {
    this.view = view;
    // Identity edits happen in Settings; always refresh the role picker
    // when returning to SOP rather than reusing a short-lived cache.
    this.identityCache = { at: 0, list: [] };
    view.webview.options = {
      enableScripts: true,
      localResourceRoots: [vscode.Uri.joinPath(this.ctx.extensionUri, 'dist')],
    };
    view.webview.html = this.buildHtml(this.ctx.extensionUri);
    this.bindDocumentListeners();
    await this.pushRefresh();
  }

  async deactivate(): Promise<void> {
    // Keep the bound YAML document and workspace listeners; the shared view may
    // switch back to SOP mode later without losing its editing context.
    this.view = undefined;
  }

  disposeView(): void {
    for (const disposable of this.disposables) {
      disposable.dispose();
    }
    this.disposables = [];
    this.documentListenersBound = false;
    this.view = undefined;
  }

  async dispatch(msg: unknown): Promise<void> {
    await this.handle(msg as BridgeRequest);
  }

  private bindDocumentListeners(): void {
    if (this.documentListenersBound) return;
    this.documentListenersBound = true;
    // The YAML text stays the source of truth: whatever changes it (typing
    // in the text editor, undo, an external tool) re-renders the canvas.
    this.disposables.push(
      vscode.workspace.onDidChangeTextDocument((event) => {
        if (
          this.document &&
          event.document.uri.toString() === this.document.uri.toString()
        ) {
          this.document = event.document;
          this.parsed = parseSopText(event.document.getText());
          void this.pushRefresh();
        }
      }),
      vscode.workspace.onDidSaveTextDocument((doc) => {
        if (this.document && doc.uri.toString() === this.document.uri.toString()) {
          void this.pushRefresh();
        }
      }),
    );
  }

  notifyLanguage(language: UiLanguage): void {
    if (!this.view) return;
    void this.view.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  /** Create (and bind to) a starter template in the first workspace folder. */
  async newSop(): Promise<void> {
    await this.createStarter();
  }

  attach(ctx: SopBridgeContext): void {
    this.ctx = ctx;
  }

  async bind(uri: vscode.Uri): Promise<void> {
    const document = await vscode.workspace.openTextDocument(uri);
    this.document = document;
    this.parsed = parseSopText(document.getText());
    await this.restoreRun();
    await this.pushRefresh();
  }

  private async createStarter(): Promise<void> {
    // A workspace folder is the happy path; without one (Extension
    // Development Host, empty window) ask for a location instead of failing.
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri;
    let target = folder
      ? vscode.Uri.joinPath(folder, 'pipeline.sop.yaml')
      : undefined;
    if (!target) {
      const picked = await vscode.window.showSaveDialog({
        saveLabel: 'Create SOP template',
        defaultUri: vscode.Uri.file(join(homedir(), 'pipeline.sop.yaml')),
        filters: { 'SOP template': ['yaml', 'yml'] },
      });
      if (!picked) {
        return; // user cancelled — not an error
      }
      target = picked;
    }
    let exists = true;
    try {
      await vscode.workspace.fs.stat(target);
    } catch {
      exists = false;
    }
    if (!exists) {
      const edit = new vscode.WorkspaceEdit();
      edit.createFile(target);
      edit.insert(target, new vscode.Position(0, 0), STARTER_YAML);
      await vscode.workspace.applyEdit(edit);
      const document = await vscode.workspace.openTextDocument(target);
      await document.save();
    }
    await this.bind(target);
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.view?.webview;
    if (!webview) return '<!DOCTYPE html><body></body>';
    const scriptUri = webview.asWebviewUri(
      vscode.Uri.joinPath(extensionUri, 'dist', 'webview-sop.js'),
    );
    const cssPath = join(extensionUri.fsPath, 'dist', 'webview-sop.css');
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
<body>
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

  private layoutKey(uri: vscode.Uri): string {
    return `agt.sop.layout:${uri.fsPath}`;
  }

  private positions(): Record<string, SopPoint> {
    if (!this.document) {
      return {};
    }
    return (
      this.ctx.context.workspaceState.get<Record<string, SopPoint>>(
        this.layoutKey(this.document.uri),
      ) ?? {}
    );
  }

  private workspaceFolder(): string | null {
    return vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
  }

  private async serverSopRuns(): Promise<RunInfo[]> {
    const cwd = this.workspaceFolder();
    const templatePath = this.document?.uri.fsPath ?? null;
    if (!cwd || !templatePath) return [];
    try {
      const list = await this.ctx.lifecycle.api.listRuns({
        mode: 'sop',
        cwd,
        templatePath,
      });
      return list.runs;
    } catch {
      return [];
    }
  }

  private async bootPayload(): Promise<Record<string, unknown>> {
    await this.discoverTemplates();
    const text = this.document?.getText() ?? '';
    const serverRuns = await this.serverSopRuns();
    const localRunIds = new Set(this.runs.map((entry) => entry.rootJobId));
    const serverOnly = serverRuns.filter((run) => !localRunIds.has(run.root_job_id));
    const runs: RunSummary[] = [
      ...this.runs.map((entry) => this.runSummary(entry, text, serverRuns)).filter((r): r is RunSummary => r !== null),
      ...serverOnly.map((run) => this.runSummaryFromServer(run, text)),
    ];
    return {
      language: this.ctx.language(),
      state: this.ctx.lifecycle.state,
      templatePath: this.document?.uri.fsPath ?? null,
      templates: this.templates,
      daemons: await this.daemonOptions(),
      identities: await this.identityOptions(),
      model: this.parsed ? this.parsed.model : null,
      text,
      dirty: this.document?.isDirty ?? false,
      positions: this.positions(),
      parseError: this.parsed?.parseError ?? null,
      workspaceFolder: this.workspaceFolder(),
      localDaemonId: this.localDaemon(),
      sidebarWidth:
        this.sidebarWidth ??
        this.ctx.context.workspaceState.get<number>(SIDEBAR_WIDTH_KEY) ??
        null,
      inspectorWidth:
        this.inspectorWidth ??
        this.ctx.context.workspaceState.get<number>(INSPECTOR_WIDTH_KEY) ??
        null,
      trackedRun: this.runSummary(this.trackedRun(), text, serverRuns),
      runs,
      run: this.runSummary(
        this.selectedRun() ?? this.trackedRun(),
        text,
        serverRuns,
      ),
    };
  }

  private trackedRun(): RunState | null {
    return this.runs.find((entry) => entry.rootJobId === this.trackedRunId) ?? null;
  }

  private selectedRun(): RunState | null {
    return this.runs.find((entry) => entry.rootJobId === this.selectedRunId) ?? null;
  }

  private runSummary(
    entry: RunState | null,
    text: string,
    serverRuns: RunInfo[],
  ): RunSummary | null {
    if (!entry) return null;
    const server = serverRuns.find((run) => run.root_job_id === entry.rootJobId);
    return this.runSummaryFromServer(
      server ?? {
        root_job_id: entry.rootJobId,
        mode: 'sop',
        status: 'unknown',
        provider: '',
        identity_did: null,
        daemon_id: null,
        cwd: this.workspaceFolder(),
        template_path: this.document?.uri.fsPath ?? null,
        template_hash: entry.specHash ?? null,
        title: entry.rootJobId,
        created_at: '',
        updated_at: '',
      },
      text,
      entry.startedAt,
      entry.specHash,
    );
  }

  private runSummaryFromServer(
    run: RunInfo,
    text: string,
    startedAt?: number,
    specHash?: string,
  ): RunSummary {
    const effectiveHash = specHash ?? run.template_hash ?? undefined;
    return {
      rootJobId: run.root_job_id,
      name: run.title ?? run.root_job_id,
      status: run.status,
      provider: run.provider,
      daemonId: run.daemon_id,
      updatedAt: run.updated_at,
      startedAt: (startedAt ?? Date.parse(run.created_at)) || Date.now(),
      stale: effectiveHash !== undefined && effectiveHash !== fingerprint(text),
      templatePath: this.document?.uri.fsPath ?? null,
      templateHash: effectiveHash ?? null,
    };
  }

  private upsertRun(entry: RunState): void {
    this.runs = [
      entry,
      ...this.runs.filter((run) => run.rootJobId !== entry.rootJobId),
    ].slice(0, 50);
  }

  private runPayload(entry: RunState): {
    rootJobId: string;
    nodes: Record<string, string>;
  } {
    return {
      rootJobId: entry.rootJobId,
      nodes: entry.nodes,
    };
  }

  /** This machine's daemon id (used for `local_daemon_id` on submit). */
  private localDaemon(): string | null {
    const cached = this.daemonCache.list;
    return localDaemonId(
      cached.map((daemon) => ({ daemon_id: daemon.id, url: daemon.url })),
      hostname(),
    );
  }

  private runKey(uri: vscode.Uri): string {
    return `agt.sop.runs:${uri.fsPath}`;
  }

  private legacyRunKey(uri: vscode.Uri): string {
    return `agt.sop.run:${uri.fsPath}`;
  }

  private async restoreRun(): Promise<void> {
    if (!this.document) {
      this.runs = [];
      this.trackedRunId = null;
      this.selectedRunId = null;
      return;
    }
    const stored =
      this.ctx.context.workspaceState.get<unknown>(
        this.runKey(this.document.uri),
      ) ??
      this.ctx.context.workspaceState.get<unknown>(
        this.legacyRunKey(this.document.uri),
      );
    if (Array.isArray(stored)) {
      this.runs = stored.filter(this.isRunState);
    } else if (this.isRunState(stored)) {
      // Migration: builds before run history stored only the latest run.
      this.runs = [stored];
    } else {
      this.runs = [];
    }
    this.trackedRunId = this.runs[0]?.rootJobId ?? null;
    this.selectedRunId = this.trackedRunId;
  }

  private async persistRun(): Promise<void> {
    if (!this.document) {
      return;
    }
    await this.ctx.context.workspaceState.update(
      this.runKey(this.document.uri),
      this.runs,
    );
  }

  private async setInspectorWidth(width: number): Promise<void> {
    this.inspectorWidth = Math.max(260, Math.min(900, Math.round(width)));
    await this.ctx.context.workspaceState.update(
      INSPECTOR_WIDTH_KEY,
      this.inspectorWidth,
    );
  }

  private async setSidebarWidth(width: number): Promise<void> {
    this.sidebarWidth = Math.max(200, Math.min(520, Math.round(width)));
    await this.ctx.context.workspaceState.update(
      SIDEBAR_WIDTH_KEY,
      this.sidebarWidth,
    );
  }

  private isRunState(value: unknown): value is RunState {
    if (typeof value !== 'object' || value === null) {
      return false;
    }
    const run = value as Partial<RunState>;
    return (
      typeof run.rootJobId === 'string' &&
      typeof run.specHash === 'string' &&
      typeof run.nodes === 'object' &&
      run.nodes !== null &&
      typeof run.startedAt === 'number'
    );
  }

  /**
   * Daemons for the inspector selects, cached briefly: the payload is
   * rebuilt on every edit, and the daemon registry does not change often.
   */
  private async daemonOptions(): Promise<SopDaemonOption[]> {
    const now = Date.now();
    if (now - this.daemonCache.at < 15_000) {
      return this.daemonCache.list;
    }
    try {
      const daemons = await this.ctx.lifecycle.api.daemons();
      this.daemonCache = {
        at: now,
        list: daemons.map((daemon) => ({
          id: daemon.daemon_id,
          label:
            daemon.daemon_name || daemon.device_name || daemon.daemon_id,
          online: daemon.online,
          providers: [...new Set(daemon.providers.map((p) => p.provider))].sort(),
          url: daemon.url ?? null,
        })),
      };
    } catch {
      // Server offline: keep the last known list so an open editor does not
      // silently lose its dropdowns.
    }
    return this.daemonCache.list;
  }

  private async identityOptions(): Promise<SopIdentityOption[]> {
    const now = Date.now();
    if (now - this.identityCache.at < 15_000) {
      return this.identityCache.list;
    }
    try {
      const identities: IdentityWithBindings[] =
        await this.ctx.lifecycle.api.identitiesWithBindings();
      this.identityCache = {
        at: now,
        list: identities
          .filter((identity) => identity.status === 'active')
          .map((identity) => ({
            did: identity.did,
            name: identity.name,
            status: identity.status,
            bindings: identity.bindings.map((binding) => ({
              daemon_id: binding.daemon_id,
              provider: binding.provider,
              priority: binding.priority,
              is_default: binding.is_default,
            })),
          })),
      };
    } catch {
      // Keep the last known identities while the server is offline.
    }
    return this.identityCache.list;
  }

  /**
   * Discover SOP documents in the workspace. An explicit ``*.sop.yaml``
   * name wins; otherwise the file must look like a SOP (top-level ``name``
   * and ``tasks``) so unrelated YAML never shows up as a template.
   */
  private async discoverTemplates(): Promise<void> {
    const found = await vscode.workspace.findFiles(
      '**/*.{yaml,yml}',
      '**/{node_modules,.git,.venv,dist,out,build}/**',
      200,
    );
    const templates: { path: string; label: string; nodeCount: number }[] = [];
    for (const uri of found) {
      let text = '';
      try {
        text = readFileSync(uri.fsPath, 'utf8');
      } catch {
        continue;
      }
      if (!/\.sop\.ya?ml$/i.test(uri.fsPath) && !looksLikeSop(text)) {
        continue;
      }
      templates.push({
        path: uri.fsPath,
        label: basename(uri.fsPath),
        nodeCount: parseSopText(text).model.nodes.length,
      });
    }
    templates.sort((a, b) => a.label.localeCompare(b.label));
    this.templates = templates;
  }

  async handle(msg: BridgeRequest): Promise<void> {
    if (!msg || msg.type !== 'request') {
      return;
    }
    try {
      let result: unknown;
      switch (msg.method) {
        case 'sop.boot':
          result = await this.bootPayload();
          break;

        case 'sop.openTemplate': {
          const path = String(msg.params?.path ?? '');
          if (path !== '') {
            await this.bind(vscode.Uri.file(path));
          }
          result = await this.bootPayload();
          break;
        }

        case 'sop.newSop': {
          await this.createStarter();
          break;
        }

        case 'sop.apply': {
          const intent = msg.params?.intent as EditIntent | undefined;
          if (this.parsed && intent) {
            this.parsed = applyIntent(this.parsed, intent);
            await this.writeDocument();
          }
          result = await this.bootPayload();
          break;
        }

        case 'sop.save': {
          if (this.document) {
            await this.document.save();
            this.parsed = parseSopText(this.document.getText());
          }
          result = await this.bootPayload();
          break;
        }

        case 'sop.validate': {
          if (!this.document) {
            result = { ok: false, error: 'no document open', at: Date.now() };
            break;
          }
          const online = await this.ctx.lifecycle.probe();
          if (!online) {
            result = { ok: false, error: null, offline: true, at: Date.now() };
            break;
          }
          try {
            const validation = await this.ctx.lifecycle.api.validateSop(
              this.document.getText(),
            );
            result = { ok: validation.ok, error: validation.error, at: Date.now() };
          } catch (err) {
            // 404 on this route means the *running* server predates it
            // (e.g. started from an older install) — say that instead of
            // surfacing a bare "HTTP 404".
            result = isMissingEndpoint(err)
              ? { ok: false, error: null, stale: true, at: Date.now() }
              : { ok: false, error: String(err), at: Date.now() };
          }
          break;
        }

        case 'sop.restartServer': {
          const ok = await this.ctx.lifecycle.restart();
          this.daemonCache = { at: 0, list: [] };
          this.identityCache = { at: 0, list: [] };
          result = { ok };
          break;
        }

        case 'sop.run': {
          if (!this.document || !this.parsed) {
            throw new Error('no-document');
          }
          const spec = this.parsed.doc.toJS();
          const submitted = await this.ctx.lifecycle.api.submitSop({
            spec,
            cwd: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
            localDaemonId: this.localDaemon(),
            templatePath: this.document.uri.fsPath,
            templateHash: fingerprint(this.document.getText()),
          });
          const entry: RunState = {
            rootJobId: submitted.root_job_id,
            specHash: fingerprint(this.document.getText()),
            nodes: submitted.nodes ?? {},
            startedAt: Date.now(),
          };
          this.upsertRun(entry);
          this.trackedRunId = entry.rootJobId;
          this.selectedRunId = entry.rootJobId;
          await this.persistRun();
          result = {
            ...this.runPayload(entry),
            ...(await this.bootPayload()),
          };
          break;
        }

        case 'sop.selectRun': {
          const rootJobId = String(msg.params?.rootJobId ?? '');
          if (!this.runs.some((run) => run.rootJobId === rootJobId)) {
            throw new Error('unknown-run');
          }
          this.selectedRunId = rootJobId;
          result = await this.bootPayload();
          break;
        }

        case 'sop.status': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') ||
            this.trackedRun()?.rootJobId ||
            '';
          if (rootJobId === '') {
            throw new Error('no-run');
          }
          const status = await this.ctx.lifecycle.api.sopStatus(rootJobId);
          result = status;
          break;
        }

        case 'sop.nodeLog': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') ||
            this.selectedRun()?.rootJobId ||
            this.trackedRun()?.rootJobId ||
            '';
          const node = String(msg.params?.node ?? '');
          if (rootJobId === '' || node === '') {
            throw new Error('no-run');
          }
          result = await this.ctx.lifecycle.api.sopNodeLog(rootJobId, node);
          break;
        }

        case 'sop.cancelRun': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') ||
            this.trackedRun()?.rootJobId ||
            '';
          if (rootJobId === '') {
            throw new Error('no-run');
          }
          const status = await this.ctx.lifecycle.api.sopStatus(rootJobId);
          const terminal = new Set(['done', 'failed', 'cancelled']);
          const pending = status.nodes.filter((n) => !terminal.has(n.status));
          await Promise.all(
            pending.map((node) => this.ctx.lifecycle.api.cancelJob(node.job_id)),
          );
          result = { cancelled: pending.map((n) => n.name) };
          break;
        }

        case 'sop.forgetRun': {
          this.trackedRunId = null;
          if (this.selectedRunId === null) {
            this.selectedRunId = this.runs[0]?.rootJobId ?? null;
          }
          await this.persistRun();
          result = await this.bootPayload();
          break;
        }

        case 'sop.revealYaml': {
          if (this.document) {
            await vscode.window.showTextDocument(this.document, { preview: false });
          }
          result = { ok: true };
          break;
        }

        case 'sop.setLayout': {
          const positions = msg.params?.positions as
            | Record<string, SopPoint>
            | undefined;
          if (positions && this.document) {
            await this.ctx.context.workspaceState.update(
              this.layoutKey(this.document.uri),
              positions,
            );
          }
          result = { ok: true };
          break;
        }

        case 'sop.setInspectorWidth': {
          const width = Number(msg.params?.width);
          if (Number.isFinite(width)) {
            await this.setInspectorWidth(width);
          }
          result = { ok: true, width: this.inspectorWidth };
          break;
        }

        case 'sop.setSidebarWidth': {
          const width = Number(msg.params?.width);
          if (Number.isFinite(width)) {
            await this.setSidebarWidth(width);
          }
          result = { ok: true, width: this.sidebarWidth };
          break;
        }

        case 'openArtifact':
          result = await openArtifact(this.ctx.lifecycle, {
            artifactUri: String(msg.params?.artifactUri ?? ''),
            dir: msg.params?.dir ? String(msg.params.dir) : null,
            workspaceFolder: msg.params?.workspaceFolder
              ? String(msg.params.workspaceFolder)
              : null,
          });
          break;

        case 'openChat':
          this.ctx.openChat();
          result = { ok: true };
          break;

        case 'openGoal':
          this.ctx.openGoal();
          result = { ok: true };
          break;

        case 'openSettings':
          this.ctx.openSettings();
          result = { ok: true };
          break;

        case 'setLanguage': {
          const language = String(msg.params?.language ?? '') as UiLanguage;
          if (language === 'zh' || language === 'en') {
            await this.ctx.setLanguage(language);
          }
          result = { ok: true };
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

  /**
   * Write the mutated document back through a ``WorkspaceEdit`` with a
   * minimal diff (common prefix/suffix trimmed), so the change lands as a
   * small edit in the buffer instead of a whole-file rewrite.
   */
  private async writeDocument(): Promise<void> {
    if (!this.document || !this.parsed) {
      return;
    }
    const next = serializeSop(this.parsed);
    const current = this.document.getText();
    if (next === current) {
      return;
    }
    const span = minimalSpan(current, next);
    const edit = new vscode.WorkspaceEdit();
    edit.replace(
      this.document.uri,
      new vscode.Range(
        this.document.positionAt(span.start),
        this.document.positionAt(span.end),
      ),
      span.text,
    );
    await vscode.workspace.applyEdit(edit);
    const reopened = vscode.workspace.textDocuments.find(
      (doc) => doc.uri.toString() === this.document?.uri.toString(),
    );
    if (reopened) {
      this.document = reopened;
      this.parsed = parseSopText(reopened.getText());
    }
  }
}
