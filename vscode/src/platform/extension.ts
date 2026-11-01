import * as vscode from 'vscode';
import { readFileSync } from 'node:fs';
import { ServerLifecycle } from '../core/lifecycle';
import type { DaemonInfo, UiLanguage } from '../core/types';
import { AgtStatusBar } from './statusbar';
import { AgtTreeProvider } from './tree';
import type { BridgeContext } from './settings-panel';
import type { ChatBridgeContext } from './chat-panel';
import type { GoalBridgeContext } from './goal-panel';
import { looksLikeSop } from '../core/sop/document';
import type { SopBridgeContext } from './sop-editor-panel';
import { WorkspacePanel } from './workspace-panel';
import { UsagePanel } from './usage-panel';
import { BackendSetup } from './backend-setup';

interface AgtConfig {
  serverUrl: string;
  autoStart: boolean;
  startCommand: string;
  stopCommand: string;
  autoStartDaemon: boolean;
  daemonStartCommand: string;
  daemonStopCommand: string;
  language: UiLanguage;
  autoInstallBackend: 'prompt' | 'off';
  agtPath: string;
}

function readConfig(): AgtConfig {
  const cfg = vscode.workspace.getConfiguration('agt');
  return {
    serverUrl: cfg.get<string>('serverUrl') ?? 'http://127.0.0.1:8420',
    autoStart: cfg.get<boolean>('autoStart') ?? true,
    // Empty means "use whatever BackendSetup detected".
    startCommand: cfg.get<string>('startCommand') ?? '',
    stopCommand: cfg.get<string>('stopCommand') ?? '',
    autoStartDaemon: cfg.get<boolean>('autoStartDaemon') ?? true,
    daemonStartCommand: cfg.get<string>('daemonStartCommand') ?? '',
    daemonStopCommand: cfg.get<string>('daemonStopCommand') ?? '',
    language: cfg.get<UiLanguage>('language') ?? 'zh',
    autoInstallBackend: cfg.get<'prompt' | 'off'>('autoInstallBackend') ?? 'prompt',
    agtPath: cfg.get<string>('agtPath') ?? '',
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

  let workspaceView: WorkspacePanel;
  let usageView: UsagePanel;
  let lastOnline: boolean | null = null;

  const backendChannel = vscode.window.createOutputChannel('AGT Backend');
  const backendSetup = new BackendSetup({
    context,
    lifecycle,
    channel: backendChannel,
    getConfig: () => config,
    language: () => language,
    workspaceRoot: () => vscode.workspace.workspaceFolders?.[0]?.uri.fsPath,
    setStatus: (status) => {
      statusbar.setBackendStatus(status);
      statusbar.update(lifecycle.state, tree.daemonCount, language);
    },
    onInstalled: () => refresh({ ensure: true }),
  });

  /**
   * Clicking a daemon or provider in the activity-bar tree jumps straight to
   * Chat and pre-selects the target.
   */
  function openChatFromTree(daemonId?: string, provider?: string): void {
    if (!daemonId) return;
    void workspaceView.show('chat', {
      chatSelection: { daemonId, provider },
    });
  }

  /**
   * Ask the Server to re-read ``~/.agt/daemons`` and reconcile its live
   * links (new targets dialed, offline ones re-dialed immediately instead
   * of waiting out the up-to-60 s backoff). Best-effort: a Server that
   * predates the endpoint, or a slow one, must not break the sidebar
   * refresh — the plain listing below still runs.
   */
  async function rescanDaemons(daemonId?: string): Promise<void> {
    try {
      await lifecycle.api.refreshDaemons(daemonId);
    } catch (err) {
      console.log(`[agt] daemon rescan failed: ${String(err)}`);
    }
  }

  async function refresh(opts?: { ensure?: boolean; rescan?: boolean }): Promise<void> {
    const online = opts?.ensure ? await lifecycle.ensureStarted() : await lifecycle.probe();
    // Reconcile *before* listing so the tree shows what the rescan found.
    if (online && opts?.rescan) await rescanDaemons();
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
    workspaceView.notifyActive(bridgeContext);
    // Usage lives in its own view; refetch only when the server comes up or
    // goes down, not on every poll.
    const onlineNow = lifecycle.state === 'online';
    if (lastOnline !== null && lastOnline !== onlineNow) {
      usageView.reload();
    }
    lastOnline = onlineNow;
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
    openSettings: () => void workspaceView.show('settings'),
    openSop: () => void openSopEditor(),
    openGoal: () => void workspaceView.show('goal'),
  };
  const sopContext: SopBridgeContext = {
    extensionUri: context.extensionUri,
    context,
    lifecycle,
    language: () => language,
    setLanguage: async (next) => {
      await vscode.workspace.getConfiguration('agt').update(
        'language',
        next,
        vscode.ConfigurationTarget.Global,
      );
    },
    openChat: () => void workspaceView.show('chat'),
    openGoal: () => void workspaceView.show('goal'),
    openSettings: () => void workspaceView.show('settings'),
  };
  const goalContext: GoalBridgeContext = {
    extensionUri: context.extensionUri,
    context,
    lifecycle,
    language: () => language,
    setLanguage: async (next) => {
      await vscode.workspace.getConfiguration('agt').update(
        'language',
        next,
        vscode.ConfigurationTarget.Global,
      );
    },
    openChat: () => void workspaceView.show('chat'),
    openSop: () => void openSopEditor(),
    openSopFile: (uri) => void openSopEditor(uri),
    openSettings: () => void workspaceView.show('settings'),
  };

  /** Only YAML that plausibly is a SOP is worth opening in the editor. */
  function isSopCandidate(uri: vscode.Uri): boolean {
    if (uri.scheme !== 'file' || !/\.ya?ml$/i.test(uri.fsPath)) {
      return false;
    }
    if (/\.sop\.ya?ml$/i.test(uri.fsPath)) {
      return true;
    }
    try {
      return looksLikeSop(readFileSync(uri.fsPath, 'utf8'));
    } catch {
      return false;
    }
  }

  function openSopEditor(uri?: vscode.Uri): void {
    let target = uri;
    if (!target) {
      const active = vscode.window.activeTextEditor?.document.uri;
      if (active && isSopCandidate(active)) {
        target = active;
      }
    }
    void workspaceView.show('sop', { sopUri: target });
  }

  workspaceView = new WorkspacePanel({
    chat: chatContext,
    sop: sopContext,
    goal: goalContext,
    settings: bridgeContext,
  });
  usageView = new UsagePanel({
    extensionUri: context.extensionUri,
    lifecycle,
    language: () => language,
  });

  const pollTimer = setInterval(() => void refresh(), 15000);

  context.subscriptions.push(
    statusbar,
    tree,
    treeView,
    vscode.commands.registerCommand('agt.treeOpenChat', (daemonId?: string, provider?: string) =>
      openChatFromTree(daemonId, provider),
    ),
    vscode.commands.registerCommand('agt.showSettings', () => void workspaceView.show('settings')),
    vscode.commands.registerCommand('agt.showChat', () => void workspaceView.show('chat')),
    vscode.commands.registerCommand('agt.showSop', () => openSopEditor()),
    vscode.commands.registerCommand('agt.showGoal', () => void workspaceView.show('goal')),
    vscode.commands.registerCommand('agt.openSopEditor', (uri?: vscode.Uri) =>
      openSopEditor(uri),
    ),
    vscode.commands.registerCommand('agt.newSop', () => void workspaceView.newSop()),
    // The sidebar title-bar refresh re-scans the Server's daemon targets,
    // not just our local view of them.
    vscode.commands.registerCommand('agt.refresh', () =>
      void refresh({ ensure: false, rescan: true }),
    ),
    vscode.commands.registerCommand('agt.startServer', () => void refresh({ ensure: true })),
    vscode.commands.registerCommand('agt.backendSetup', () =>
      void backendSetup.requestInstall(),
    ),
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
    vscode.window.registerWebviewViewProvider('agt.workspace', workspaceView),
    vscode.window.registerWebviewViewProvider('agt.usage', usageView),
    usageView,
    backendChannel,
    vscode.window.onDidChangeActiveTextEditor(() => workspaceView.notifyWorkspaceRoot()),
    vscode.workspace.onDidChangeWorkspaceFolders(() => workspaceView.notifyWorkspaceRoot()),
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (!e.affectsConfiguration('agt')) {
        return;
      }
      config = readConfig();
      language = config.language;
      // Commands are re-resolved by BackendSetup; passing them here would
      // overwrite the detected values with the (possibly empty) settings.
      lifecycle.updateOptions({
        serverUrl: config.serverUrl,
        autoStart: config.autoStart,
        autoStartDaemon: config.autoStartDaemon,
      });
      workspaceView.notifyLanguage(config.language);
      usageView.notifyLanguage(config.language);
      void backendSetup.reconfigure().then(() => refresh());
    }),
    { dispose: () => clearInterval(pollTimer) },
  );

  // Detect/install the backend before the first ensureStarted(): a missing
  // backend must produce a prompt, not a silent failed start.
  void (async () => {
    await backendSetup.initialize();
    await refresh({ ensure: config.autoStart });
  })();
}

export function deactivate(): void {
  // Polling and views are disposed via context.subscriptions.
}
