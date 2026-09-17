import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { homedir, hostname } from 'node:os';
import { basename, join } from 'node:path';
import { isMissingEndpoint } from '../core/api';
import type { ServerLifecycle } from '../core/lifecycle';
import { looksLikeSop, minimalSpan } from '../core/sop/document';
import type { SopPoint } from '../core/sop/layout';
import type { EditIntent } from '../core/sop/model';
import { fingerprint, localDaemonId } from '../core/sop/run';
import type { SopStatus } from '../core/types';
import {
  applyIntent,
  parseSopText,
  serializeSop,
  type ParsedSop,
} from '../core/sop/yaml-doc';
import type { UiLanguage } from '../core/types';

export interface SopBridgeContext {
  extensionUri: vscode.Uri;
  context: vscode.ExtensionContext;
  lifecycle: ServerLifecycle;
  language(): UiLanguage;
  setLanguage(language: UiLanguage): Promise<void>;
  openChat(): void;
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

interface RunState {
  rootJobId: string;
  /** Fingerprint of the submitted document text. */
  specHash: string;
  nodes: Record<string, string>;
  startedAt: number;
}

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
  private static current: SopEditorPanel | undefined;

  static show(ctx: SopBridgeContext, uri?: vscode.Uri): void {
    if (SopEditorPanel.current) {
      SopEditorPanel.current.attach(ctx);
      SopEditorPanel.current.panel.reveal();
      if (uri) {
        void SopEditorPanel.current.bind(uri);
      } else {
        void SopEditorPanel.current.pushRefresh();
      }
      return;
    }
    SopEditorPanel.current = new SopEditorPanel(ctx);
    if (uri) {
      void SopEditorPanel.current.bind(uri);
    }
  }

  static notifyLanguage(language: UiLanguage): void {
    void SopEditorPanel.current?.panel.webview.postMessage({
      type: 'event',
      event: 'language',
      payload: language,
    });
  }

  /** Path of the document currently bound to the panel (or null). */
  static boundPath(): string | null {
    return SopEditorPanel.current?.document?.uri.fsPath ?? null;
  }

  /** Create (and bind to) a starter template in the first workspace folder. */
  static async newSop(ctx: SopBridgeContext): Promise<void> {
    SopEditorPanel.show(ctx);
    await SopEditorPanel.current?.createStarter();
  }

  private readonly panel: vscode.WebviewPanel;
  private ctx: SopBridgeContext;
  private document: vscode.TextDocument | null = null;
  private parsed: ParsedSop | null = null;
  private disposables: vscode.Disposable[] = [];
  private templates: { path: string; label: string; nodeCount: number }[] = [];
  private daemonCache: { at: number; list: SopDaemonOption[] } = { at: 0, list: [] };
  private run: RunState | null = null;
  private lastStatus: SopStatus | null = null;

  private constructor(ctx: SopBridgeContext) {
    this.ctx = ctx;
    this.panel = vscode.window.createWebviewPanel(
      'agt.sopEditor',
      'AGT SOP Editor',
      vscode.ViewColumn.Active,
      {
        enableScripts: true,
        retainContextWhenHidden: true,
        localResourceRoots: [vscode.Uri.joinPath(ctx.extensionUri, 'dist')],
      },
    );
    this.panel.webview.html = this.buildHtml(ctx.extensionUri);
    this.panel.webview.onDidReceiveMessage((msg) =>
      void this.handle(msg as BridgeRequest),
    );
    this.panel.onDidDispose(() => {
      for (const disposable of this.disposables) {
        disposable.dispose();
      }
      this.disposables = [];
      if (SopEditorPanel.current === this) {
        SopEditorPanel.current = undefined;
      }
    });

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
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument(target), {
      preview: false,
    });
  }

  private buildHtml(extensionUri: vscode.Uri): string {
    const webview = this.panel.webview;
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
    await this.panel.webview.postMessage({
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

  private async bootPayload(): Promise<Record<string, unknown>> {
    await this.discoverTemplates();
    const text = this.document?.getText() ?? '';
    return {
      language: this.ctx.language(),
      state: this.ctx.lifecycle.state,
      templatePath: this.document?.uri.fsPath ?? null,
      templates: this.templates,
      daemons: await this.daemonOptions(),
      model: this.parsed ? this.parsed.model : null,
      text,
      dirty: this.document?.isDirty ?? false,
      positions: this.positions(),
      parseError: this.parsed?.parseError ?? null,
      workspaceFolder: vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null,
      localDaemonId: this.localDaemon(),
      run: this.run
        ? {
            rootJobId: this.run.rootJobId,
            startedAt: this.run.startedAt,
            stale: this.run.specHash !== fingerprint(text),
          }
        : null,
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
    return `agt.sop.run:${uri.fsPath}`;
  }

  private async restoreRun(): Promise<void> {
    if (!this.document) {
      this.run = null;
      return;
    }
    const stored = this.ctx.context.workspaceState.get<RunState>(
      this.runKey(this.document.uri),
    );
    this.run = stored ?? null;
    this.lastStatus = null;
  }

  private async persistRun(): Promise<void> {
    if (!this.document) {
      return;
    }
    await this.ctx.context.workspaceState.update(
      this.runKey(this.document.uri),
      this.run,
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

  private async handle(msg: BridgeRequest): Promise<void> {
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
          });
          this.run = {
            rootJobId: submitted.root_job_id,
            specHash: fingerprint(this.document.getText()),
            nodes: submitted.nodes ?? {},
            startedAt: Date.now(),
          };
          this.lastStatus = null;
          await this.persistRun();
          result = {
            rootJobId: this.run.rootJobId,
            nodes: this.run.nodes,
            ...(await this.bootPayload()),
          };
          break;
        }

        case 'sop.status': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') || this.run?.rootJobId || '';
          if (rootJobId === '') {
            throw new Error('no-run');
          }
          const status = await this.ctx.lifecycle.api.sopStatus(rootJobId);
          this.lastStatus = status;
          result = status;
          break;
        }

        case 'sop.nodeLog': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') || this.run?.rootJobId || '';
          const node = String(msg.params?.node ?? '');
          if (rootJobId === '' || node === '') {
            throw new Error('no-run');
          }
          result = await this.ctx.lifecycle.api.sopNodeLog(rootJobId, node);
          break;
        }

        case 'sop.cancelRun': {
          const rootJobId =
            String(msg.params?.rootJobId ?? '') || this.run?.rootJobId || '';
          if (rootJobId === '') {
            throw new Error('no-run');
          }
          const status =
            this.lastStatus ?? (await this.ctx.lifecycle.api.sopStatus(rootJobId));
          const terminal = new Set(['done', 'failed', 'cancelled']);
          const pending = status.nodes.filter((n) => !terminal.has(n.status));
          await Promise.all(
            pending.map((node) => this.ctx.lifecycle.api.cancelJob(node.job_id)),
          );
          this.lastStatus = null;
          result = { cancelled: pending.map((n) => n.name) };
          break;
        }

        case 'sop.forgetRun': {
          this.run = null;
          this.lastStatus = null;
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

        case 'openChat':
          this.ctx.openChat();
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
      await this.panel.webview.postMessage({ type: 'response', id: msg.id, result });
    } catch (err) {
      await this.panel.webview.postMessage({
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
