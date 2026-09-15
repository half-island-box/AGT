import type { UiLanguage } from './types';

/**
 * Static-UI dictionary. Dynamic data (daemon names, providers, statuses)
 * is intentionally not translated — it comes from the backend as-is.
 */
const zh = {
  'app.title': 'AGT',
  'app.subtitle': 'multi-agent bridge',
  'status.online': '在线',
  'status.offline': '离线',
  'status.starting': '启动中',
  'status.unknown': '未知',
  'statusbar.online': 'AGT · {count} 个 daemon 在线',
  'statusbar.offline': 'AGT · server 离线',
  'statusbar.starting': 'AGT · server 启动中…',
  'tree.server': 'Server',
  'tree.daemons': 'Daemons',
  'tree.noDaemons': '暂无 daemon',
  'tree.runtimes': '{count} 个运行时',
  'settings.title': '设置',
  'settings.connection': '连接与 Server',
  'settings.serverUrl': 'Server URL',
  'settings.test': '测试连接',
  'settings.connected': '已连接 · {ms}ms',
  'settings.disconnected': '未连接',
  'settings.autoStart': '窗口启动时自动拉起 agt server',
  'settings.startCommand': '启动命令',
  'settings.stopCommand': '停止命令',
  'settings.daemons': 'Daemons',
  'settings.host': '主机',
  'settings.providers': 'Providers',
  'settings.status': '状态',
  'settings.lastSeen': '最近心跳',
  'settings.addDaemon': '添加 daemon',
  'settings.daemonHint': 'daemon 侧执行：{command}（支持 NAT 后反向拨号）',
  'settings.startServer': '启动 Server',
  'settings.stopServer': '停止 Server',
  'settings.langToggle': '中 / EN',
  'settings.daemonName': '名称',
  'settings.daemonUrl': '反向拨号地址',
  'settings.daemonCaps': '能力标签',
  'settings.daemonCapsPh': '逗号分隔，可选',
  'settings.addDaemonSubmit': '写入配置',
  'settings.cancel': '取消',
  'settings.addDaemonOk': '已写入 {path}。重启 server 生效后，在 daemon 机器上执行：',
  'settings.addDaemonDup': '该地址已存在于 daemons 文件',
  'settings.addDaemonBadUrl': '地址必须以 ws:// 或 wss:// 开头',
  'settings.addDaemonNeedName': '名称不能为空',
  'settings.addDaemonBadName': '名称只能包含字母、数字、点、下划线和连字符',
  'settings.copy': '复制',
  'settings.copied': '已复制',
};

const en: Record<keyof typeof zh, string> = {
  'app.title': 'AGT',
  'app.subtitle': 'multi-agent bridge',
  'status.online': 'online',
  'status.offline': 'offline',
  'status.starting': 'starting',
  'status.unknown': 'unknown',
  'statusbar.online': 'AGT · {count} daemons online',
  'statusbar.offline': 'AGT · server offline',
  'statusbar.starting': 'AGT · starting server…',
  'tree.server': 'Server',
  'tree.daemons': 'Daemons',
  'tree.noDaemons': 'No daemons yet',
  'tree.runtimes': '{count} runtimes',
  'settings.title': 'Settings',
  'settings.connection': 'Connection & server',
  'settings.serverUrl': 'Server URL',
  'settings.test': 'Test',
  'settings.connected': 'Connected · {ms}ms',
  'settings.disconnected': 'Disconnected',
  'settings.autoStart': 'Auto-start agt server on window startup',
  'settings.startCommand': 'Start command',
  'settings.stopCommand': 'Stop command',
  'settings.daemons': 'Daemons',
  'settings.host': 'Host',
  'settings.providers': 'Providers',
  'settings.status': 'Status',
  'settings.lastSeen': 'Last seen',
  'settings.addDaemon': 'Add daemon',
  'settings.daemonHint': 'Run on the daemon side: {command} (reverse-dial behind NAT supported)',
  'settings.startServer': 'Start server',
  'settings.stopServer': 'Stop server',
  'settings.langToggle': '中 / EN',
  'settings.daemonName': 'Name',
  'settings.daemonUrl': 'Reverse-dial URL',
  'settings.daemonCaps': 'Capabilities',
  'settings.daemonCapsPh': 'comma-separated, optional',
  'settings.addDaemonSubmit': 'Save entry',
  'settings.cancel': 'Cancel',
  'settings.addDaemonOk': 'Written to {path}. After restarting the server, run on the daemon machine:',
  'settings.addDaemonDup': 'This URL already exists in the daemons file',
  'settings.addDaemonBadUrl': 'URL must start with ws:// or wss://',
  'settings.addDaemonNeedName': 'Name is required',
  'settings.addDaemonBadName': 'Name may only contain letters, digits, dots, underscores, and hyphens',
  'settings.copy': 'Copy',
  'settings.copied': 'Copied',
};

const dictionaries: Record<UiLanguage, Record<string, string>> = { zh, en };

export type MessageKey = keyof typeof zh;

export function t(lang: UiLanguage, key: MessageKey, vars?: Record<string, string | number>): string {
  let text = dictionaries[lang][key] ?? dictionaries.zh[key] ?? key;
  if (vars) {
    for (const [name, value] of Object.entries(vars)) {
      text = text.replaceAll(`{${name}}`, String(value));
    }
  }
  return text;
}
