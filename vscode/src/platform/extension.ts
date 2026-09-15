import * as vscode from 'vscode';
import { ServerLifecycle } from '../core/lifecycle';
import type { DaemonInfo, UiLanguage } from '../core/types';
import { AgtStatusBar } from './statusbar';
import { AgtTreeProvider } from './tree';
import { SettingsPanel, type BridgeContext } from './settings-panel';

interface AgtConfig {
  serverUrl: string;
  autoStart: boolean;
  startCommand: string;
  stopCommand: string;
  language: UiLanguage;
}

function readConfig(): AgtConfig {
  const cfg = vscode.workspace.getConfiguration('agt');
  return {
    serverUrl: cfg.get<string>('serverUrl') ?? 'http://127.0.0.1:8420',
    autoStart: cfg.get<boolean>('autoStart') ?? true,
    startCommand: cfg.get<string>('startCommand') ?? 'uv run agt server start --detach',
    stopCommand: cfg.get<string>('stopCommand') ?? 'uv run agt server stop',
    language: cfg.get<UiLanguage>('language') ?? 'zh',
  };
}

export function activate(context: vscode.ExtensionContext): void {
  let config = readConfig();
  let language = config.language;

  const lifecycle = new ServerLifecycle({
    serverUrl: config.serverUrl,
    startCommand: config.startCommand,
    stopCommand: config.stopCommand,
    autoStart: config.autoStart,
    logger: (message) => console.log(`[agt] ${message}`),
  });

  const statusbar = new AgtStatusBar();
  const tree = new AgtTreeProvider(() => language);
  const treeView = vscode.window.createTreeView('agt.daemons', {
    treeDataProvider: tree,
  });

  const getDaemons = async (): Promise<DaemonInfo[]> => {
    try {
      return await lifecycle.api.daemons();
    } catch {
      return [];
    }
  };

  async function refresh(opts?: { ensure?: boolean }): Promise<void> {
    const online = opts?.ensure ? await lifecycle.ensureStarted() : await lifecycle.probe();
    const daemons = online ? await getDaemons() : undefined;
    tree.setDaemons(daemons);
    tree.setServerInfo(lifecycle.state, config.serverUrl, lifecycle.latencyMs);
    statusbar.update(lifecycle.state, tree.daemonCount, language);
    SettingsPanel.notifyActive(bridgeContext);
  }

  const bridgeContext: BridgeContext = {
    extensionUri: context.extensionUri,
    lifecycle,
    language: () => language,
    getDaemons,
    onConfigOrStateChange: () => void refresh(),
  };

  const pollTimer = setInterval(() => void refresh(), 15000);

  context.subscriptions.push(
    statusbar,
    tree,
    treeView,
    vscode.commands.registerCommand('agt.showSettings', () => SettingsPanel.show(bridgeContext)),
    vscode.commands.registerCommand('agt.refresh', () => void refresh({ ensure: false })),
    vscode.commands.registerCommand('agt.startServer', () => void refresh({ ensure: true })),
    vscode.commands.registerCommand('agt.stopServer', async () => {
      await lifecycle.stop();
      await refresh();
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('agt')) {
        return;
      }
      config = readConfig();
      language = config.language;
      lifecycle.updateOptions(config);
      void refresh();
    }),
    { dispose: () => clearInterval(pollTimer) },
  );

  void refresh({ ensure: config.autoStart });
}

export function deactivate(): void {
  // Polling and views are disposed via context.subscriptions.
}
