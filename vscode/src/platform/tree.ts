import * as vscode from 'vscode';
import { providerIconDataUrl } from '../core/icons';
import { t } from '../core/i18n';
import type { DaemonInfo, DaemonProvider, ServerState, UiLanguage } from '../core/types';

export type TreeNode =
  | { kind: 'server' }
  | { kind: 'daemon'; daemon: DaemonInfo }
  | { kind: 'provider'; daemonId: string; provider: DaemonProvider };

const FALLBACK_PROVIDERS: Record<string, { icon: string; color: string }> = {
  claude: { icon: 'hubot', color: 'debugIcon.breakpointForeground' },
  codex: { icon: 'copilot', color: 'debugIcon.startForeground' },
  qwen: { icon: 'sparkle', color: 'debugIcon.pauseForeground' },
  kimi: { icon: 'comment', color: 'debugIcon.startForeground' },
  copilot: { icon: 'copilot', color: 'debugIcon.breakpointForeground' },
  cursor: { icon: 'edit', color: 'descriptionForeground' },
  opencode: { icon: 'code', color: 'debugIcon.startForeground' },
  openclaw: { icon: 'claw', color: 'debugIcon.pauseForeground' },
};

function providerIcon(provider: string): vscode.Uri | vscode.ThemeIcon {
  const dataUrl = providerIconDataUrl(provider);
  if (dataUrl) {
    return vscode.Uri.parse(dataUrl);
  }
  const key = provider.toLowerCase();
  const meta = FALLBACK_PROVIDERS[key] ?? { icon: 'terminal', color: 'descriptionForeground' };
  return new vscode.ThemeIcon(meta.icon, new vscode.ThemeColor(meta.color));
}

export class AgtTreeProvider implements vscode.TreeDataProvider<TreeNode>, vscode.Disposable {
  private readonly changeEmitter = new vscode.EventEmitter<TreeNode | undefined>();
  readonly onDidChangeTreeData = this.changeEmitter.event;

  private daemons: DaemonInfo[] = [];
  private serverState: ServerState = 'unknown';
  private serverUrl = '';
  private latencyMs: number | null = null;

  constructor(private readonly lang: () => UiLanguage) {}

  setServerInfo(state: ServerState, url: string, latencyMs: number | null): void {
    this.serverState = state;
    this.serverUrl = url;
    this.latencyMs = latencyMs;
    this.fire();
  }

  setDaemons(daemons: DaemonInfo[] | undefined): void {
    this.daemons = daemons ?? [];
    this.fire();
  }

  get daemonCount(): number {
    return this.daemons.length;
  }

  getTreeItem(node: TreeNode): vscode.TreeItem {
    const lang = this.lang();
    if (node.kind === 'server') {
      const item = new vscode.TreeItem(t(lang, 'tree.server'), vscode.TreeItemCollapsibleState.None);
      const stateKey = `status.${this.serverState}` as const;
      const stateText = t(lang, stateKey);
      item.description =
        this.serverState === 'online' && this.latencyMs !== null
          ? `${stateText} · ${this.latencyMs}ms`
          : stateText;
      item.tooltip = this.serverUrl;
      item.iconPath = new vscode.ThemeIcon(
        this.serverState === 'online' ? 'server' : 'server-process',
        this.serverState === 'online'
          ? new vscode.ThemeColor('testing.iconPassed')
          : this.serverState === 'starting'
            ? new vscode.ThemeColor('debugIcon.pauseForeground')
            : new vscode.ThemeColor('testing.iconFailed'),
      );
      item.contextValue = 'server';
      return item;
    }
    if (node.kind === 'daemon') {
      const d = node.daemon;
      const item = new vscode.TreeItem(d.daemon_id, vscode.TreeItemCollapsibleState.Expanded);
      item.description = d.device_name;
      item.tooltip = new vscode.MarkdownString(
        [
          `**${d.daemon_id}**`,
          d.daemon_name ?? '',
          d.url ? `\`${d.url}\`` : '',
          `${t(lang, 'tree.runtimes', { count: d.runtime_count })} · last seen ${d.last_seen}`,
          d.capabilities.length ? `capabilities: ${d.capabilities.join(', ')}` : '',
        ]
          .filter(Boolean)
          .join('\n\n'),
      );
      item.iconPath = new vscode.ThemeIcon(
        d.online ? 'circle-filled' : 'circle-outline',
        d.online ? new vscode.ThemeColor('testing.iconPassed') : new vscode.ThemeColor('testing.iconFailed'),
      );
      item.contextValue = d.online ? 'daemon-online' : 'daemon-offline';
      item.command = {
        command: 'agt.treeOpenChat',
        title: t(lang, 'chat.title'),
        arguments: [d.daemon_id],
      };
      return item;
    }
    const p = node.provider;
    const item = new vscode.TreeItem(p.provider, vscode.TreeItemCollapsibleState.None);
    item.description = `${p.name} · ${p.version}`;
    item.tooltip = new vscode.MarkdownString(
      [`**${p.provider}**`, `${p.name} ${p.version}`, `status: ${p.status}`].join('\n\n'),
    );
    item.iconPath = providerIcon(p.provider);
    item.command = {
      command: 'agt.treeOpenChat',
      title: t(lang, 'chat.title'),
      arguments: [node.daemonId, p.provider],
    };
    return item;
  }

  getChildren(element?: TreeNode): TreeNode[] {
    if (!element) {
      const server: TreeNode = { kind: 'server' };
      const daemons: TreeNode[] = this.daemons.map((d) => ({ kind: 'daemon', daemon: d }));
      return [server, ...daemons];
    }
    if (element.kind === 'daemon') {
      return element.daemon.providers.map((p) => ({
        kind: 'provider',
        daemonId: element.daemon.daemon_id,
        provider: p,
      }));
    }
    return [];
  }

  private fire(): void {
    this.changeEmitter.fire(undefined);
  }

  dispose(): void {
    this.changeEmitter.dispose();
  }
}
