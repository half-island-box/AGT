import * as vscode from 'vscode';
import { t } from '../core/i18n';
import type { ServerState, UiLanguage } from '../core/types';

/** Whether the plugin found (or installed) a usable backend on this machine. */
export type BackendStatus = 'unknown' | 'missing' | 'installing' | 'ready';

export class AgtStatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;
  private backend: BackendStatus = 'unknown';

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 60);
    this.item.name = 'AGT';
    this.item.command = 'agt.showSettings';
    this.item.show();
  }

  setBackendStatus(status: BackendStatus): void {
    this.backend = status;
  }

  update(state: ServerState, daemonCount: number, lang: UiLanguage): void {
    // A missing/installing backend is the more actionable state: the server
    // cannot come online until it is resolved, so it wins over "offline".
    if (this.backend === 'installing') {
      this.item.text = `$(sync~spin) ${t(lang, 'backend.statusInstalling')}`;
      this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
      this.item.tooltip = t(lang, 'backend.statusInstalling');
      this.item.command = 'agt.backendSetup';
      return;
    }
    if (this.backend === 'missing' && state !== 'online') {
      this.item.text = `$(warning) ${t(lang, 'backend.statusMissing')}`;
      this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
      this.item.tooltip = t(lang, 'backend.statusMissingTip');
      this.item.command = 'agt.backendSetup';
      return;
    }
    this.item.command = 'agt.showSettings';
    switch (state) {
      case 'online':
        this.item.text = `$(check) ${t(lang, 'statusbar.online', { count: daemonCount })}`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.prominentBackground');
        this.item.tooltip = t(lang, 'status.online');
        break;
      case 'starting':
        this.item.text = `$(sync~spin) ${t(lang, 'statusbar.starting')}`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.warningBackground');
        this.item.tooltip = t(lang, 'status.starting');
        break;
      default:
        this.item.text = `$(circle-slash) ${t(lang, 'statusbar.offline')}`;
        this.item.backgroundColor = new vscode.ThemeColor('statusBarItem.errorBackground');
        this.item.tooltip = t(lang, 'status.offline');
        break;
    }
  }

  dispose(): void {
    this.item.dispose();
  }
}
