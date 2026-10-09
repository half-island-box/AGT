import * as vscode from 'vscode';
import { ChatPanel, type ChatBridgeContext } from './chat-panel';
import { GoalPanel, type GoalBridgeContext } from './goal-panel';
import { SettingsPanel, type BridgeContext } from './settings-panel';
import { SopEditorPanel, type SopBridgeContext } from './sop-editor-panel';

export type WorkspaceMode = 'chat' | 'sop' | 'goal' | 'settings';

interface BridgeMessage {
  type: 'request';
  id: number;
  method: string;
  params?: Record<string, unknown>;
}

interface WorkspacePanelContext {
  chat: ChatBridgeContext;
  sop: SopBridgeContext;
  goal: GoalBridgeContext;
  settings: BridgeContext;
}

interface PendingReveal {
  mode: WorkspaceMode;
  sopUri?: vscode.Uri;
}

/**
 * One docked webview with mode-specific bundles.
 *
 * VS Code would stack four contributed views in one container. Keeping one
 * view and swapping its HTML here makes the left-top mode switcher behave like
 * an application shell: exactly one AGT page is visible at a time.
 */
export class WorkspacePanel implements vscode.WebviewViewProvider {
  private view: vscode.WebviewView | undefined;
  private mode: WorkspaceMode = 'chat';
  private pendingReveal: PendingReveal | undefined;
  private readonly chat: ChatPanel;
  private readonly sop: SopEditorPanel;
  private readonly goal: GoalPanel;
  private readonly settings: SettingsPanel;

  constructor(ctx: WorkspacePanelContext) {
    this.chat = new ChatPanel(ctx.chat);
    this.sop = new SopEditorPanel(ctx.sop);
    this.goal = new GoalPanel(ctx.goal);
    this.settings = new SettingsPanel(ctx.settings);
  }

  async resolveWebviewView(
    view: vscode.WebviewView,
    _context: vscode.WebviewViewResolveContext,
    _token: vscode.CancellationToken,
  ): Promise<void> {
    this.view = view;
    const pending = this.pendingReveal ?? { mode: this.mode };
    this.pendingReveal = undefined;
    view.webview.onDidReceiveMessage((msg) => void this.handleMessage(msg));
    view.onDidDispose(() => this.disposeViews());
    await this.present(pending.mode, pending.sopUri);
  }

  async show(mode: WorkspaceMode, sopUri?: vscode.Uri): Promise<void> {
    if (!this.view) {
      this.pendingReveal = { mode, sopUri };
      await vscode.commands.executeCommand('agt.workspace.focus');
      return;
    }
    this.view.show(true);
    if (this.mode === mode) {
      if (mode === 'sop' && sopUri) {
        await this.sop.bind(sopUri);
      }
      return;
    }
    await this.controller(this.mode).deactivate();
    this.mode = mode;
    await this.controller(mode).present(this.view);
    if (mode === 'sop' && sopUri) {
      await this.sop.bind(sopUri);
    }
  }

  async newSop(): Promise<void> {
    await this.show('sop');
    await this.sop.newSop();
  }

  notifyActive(ctx: BridgeContext): void {
    this.settings.notifyActive(ctx);
  }

  notifyLanguage(language: Parameters<ChatPanel['notifyLanguage']>[0]): void {
    if (this.mode === 'settings') {
      // The settings payload already carries the current language.
      return;
    }
    switch (this.mode) {
      case 'chat':
        this.chat.notifyLanguage(language);
        break;
      case 'sop':
        this.sop.notifyLanguage(language);
        break;
      case 'goal':
        this.goal.notifyLanguage(language);
        break;
    }
  }

  private controller(mode: WorkspaceMode) {
    switch (mode) {
      case 'chat':
        return this.chat;
      case 'sop':
        return this.sop;
      case 'goal':
        return this.goal;
      case 'settings':
        return this.settings;
    }
  }

  private async present(mode: WorkspaceMode, sopUri?: vscode.Uri): Promise<void> {
    if (!this.view) return;
    this.mode = mode;
    await this.controller(mode).present(this.view);
    if (mode === 'sop' && sopUri) {
      await this.sop.bind(sopUri);
    }
  }

  private async handleMessage(msg: unknown): Promise<void> {
    const request = msg as BridgeMessage | undefined;
    if (!request || request.type !== 'request') return;

    switch (request.method) {
      case 'openChat':
        await this.show('chat');
        break;
      case 'openSop':
        await this.show('sop');
        break;
      case 'openGoal':
        await this.show('goal');
        break;
      case 'openSettings':
        await this.show('settings');
        break;
      default:
        await this.controller(this.mode).dispatch(request);
        return;
    }
    await this.view?.webview.postMessage({
      type: 'response',
      id: request.id,
      result: { ok: true },
    });
  }

  private disposeViews(): void {
    void this.chat.deactivate();
    this.sop.disposeView();
    void this.goal.deactivate();
    void this.settings.deactivate();
    this.view = undefined;
  }
}
