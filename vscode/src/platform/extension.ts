import * as vscode from 'vscode';
import { ServerLifecycle } from '../core/lifecycle';
import type { DaemonInfo, UiLanguage } from '../core/types';
import { AgtStatusBar } from './statusbar';
import { AgtTreeProvider } from './tree';
import { SettingsPanel, type BridgeContext } from './settings-panel';
import { ChatPanel, type ChatBridgeContext } from './chat-panel';

interface AgtConfig {
  serverUrl: string;
  autoStart: boolean;
  startCommand: string;
  stopCommand: string;
  autoStartDaemon: boolean;
  daemonStartCommand: string;
  daemonStopCommand: string;
  language: UiLanguage;
}

function readConfig(): AgtConfig {
  const cfg = vscode.workspace.getConfiguration('agt');
  return {
    serverUrl: cfg.get<string>('serverUrl') ?? 'http://127.0.0.1:8420',
    autoStart: cfg.get<boolean>('autoStart') ?? true,
    startCommand: cfg.get<string>('startCommand') ?? 'uv run agt server start --detach',
    stopCommand: cfg.get<string>('stopCommand') ?? 'uv run agt server stop',
    autoStartDaemon: cfg.get<boolean>('autoStartDaemon') ?? true,
    daemonStartCommand:
      cfg.get<string>('daemonStartCommand') ?? 'uv run agt daemon start --detach',
    daemonStopCommand: cfg.get<string>('daemonStopCommand') ?? 'uv run agt daemon stop',
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
    autoStartDaemon: config.autoStartDaemon,
    daemonStartCommand: config.daemonStartCommand,
    daemonStopCommand: config.daemonStopCommand,
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
    if (online && opts?.ensure) {
      // A server we just started should run with a local daemon; don't make
      // the tree/status bar wait for the daemon's own startup.
      void lifecycle.ensureDaemonStarted().then((started) => {
        if (started) {
          void refresh();
        }
      });
    }
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
  const chatContext: ChatBridgeContext = {
    extensionUri: context.extensionUri,
    lifecycle,
    language: () => language,
    setLanguage: async (next) => {
      await vscode.workspace.getConfiguration('agt').update(
        'language',
        next,
        vscode.ConfigurationTarget.Global,
      );
    },
    openSettings: () => SettingsPanel.show(bridgeContext),
  };

  const pollTimer = setInterval(() => void refresh(), 15000);

  context.subscriptions.push(
    statusbar,
    tree,
    treeView,
    vscode.commands.registerCommand('agt.showSettings', () => SettingsPanel.show(bridgeContext)),
    vscode.commands.registerCommand('agt.showChat', () => ChatPanel.show(chatContext)),
    vscode.commands.registerCommand('agt.refresh', () => void refresh({ ensure: false })),
    vscode.commands.registerCommand('agt.startServer', () => void refresh({ ensure: true })),
    vscode.commands.registerCommand('agt.stopServer', async () => {
      await lifecycle.stop();
      await refresh();
    }),
    vscode.commands.registerCommand('agt.startDaemon', async () => {
      await lifecycle.startDaemon();
      await refresh();
    }),
    vscode.commands.registerCommand('agt.stopDaemon', async () => {
      await lifecycle.stopDaemon();
      await refresh();
    }),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('agt')) {
        return;
      }
      config = readConfig();
      language = config.language;
      lifecycle.updateOptions(config);
      ChatPanel.notifyLanguage(config.language);
      void refresh();
    }),
    { dispose: () => clearInterval(pollTimer) },
  );

  void refresh({ ensure: config.autoStart });
}

export function deactivate(): void {
  // Polling and views are disposed via context.subscriptions.
}
