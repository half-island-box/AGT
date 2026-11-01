import * as vscode from 'vscode';
import { t } from '../core/i18n';
import type { ServerState, UiLanguage } from '../core/types';

export class AgtStatusBar implements vscode.Disposable {
  private readonly item: vscode.StatusBarItem;

  constructor() {
    this.item = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 60);
    this.item.name = 'AGT';
    this.item.command = 'agt.showSettings';
    this.item.show();
  }

  update(state: ServerState, daemonCount: number, lang: UiLanguage): void {
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
