import type { DaemonInfo, ServerState } from '../core/types';

/** Fixture data + fake latency for browser preview (`npm run dev:web`). */
const daemons: DaemonInfo[] = [
  {
    daemon_id: 'dev-box-1',
    daemon_name: '本地开发机',
    device_name: 'local',
    providers: [
      { provider: 'claude', name: 'Claude Code', version: '1.0.60', status: 'online', last_seen: '2026-09-15 10:00:01' },
      { provider: 'codex', name: 'Codex CLI', version: '0.20.0', status: 'online', last_seen: '2026-09-15 10:00:01' },
      { provider: 'qwen', name: 'Qwen Code', version: '0.7.1', status: 'online', last_seen: '2026-09-15 10:00:01' },
    ],
    runtime_count: 3,
    last_seen: '2026-09-15 10:00:01',
    online: true,
    url: null,
    capabilities: ['local'],
  },
  {
    daemon_id: 'dev-box-2',
    daemon_name: null,
    device_name: '192.168.1.24',
    providers: [
      { provider: 'kimi', name: 'Kimi CLI', version: '1.2.3', status: 'online', last_seen: '2026-09-15 09:59:58' },
      { provider: 'codex', name: 'Codex CLI', version: '0.20.0', status: 'online', last_seen: '2026-09-15 09:59:58' },
    ],
    runtime_count: 2,
    last_seen: '2026-09-15 09:59:58',
    online: true,
    url: 'ws://192.168.1.24:0/rev',
    capabilities: ['rust', 'review'],
  },
  {
    daemon_id: 'ci-runner',
    daemon_name: null,
    device_name: '10.0.4.11',
    providers: [
      { provider: 'opencode', name: 'OpenCode', version: '0.8.0', status: 'offline', last_seen: '2026-09-15 08:02:00' },
    ],
    runtime_count: 1,
    last_seen: '2026-09-15 08:02:00',
    online: false,
    url: null,
    capabilities: [],
  },
];

let state: ServerState = 'online';
let language = 'zh';
let serverUrl = 'http://127.0.0.1:8420';
let autoStart = true;
const startCommand = 'uv run agt server start --detach';
const stopCommand = 'uv run agt server stop';

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function boot(): Record<string, unknown> {
  return {
    serverUrl,
    autoStart,
    startCommand,
    stopCommand,
    language,
    state,
    latencyMs: state === 'online' ? 12 : null,
    daemons: state === 'online' ? daemons : [],
  };
}

export async function mockCall<T>(_method: string, params?: Record<string, unknown>): Promise<T> {
  await delay(200 + Math.random() * 200);
  const method = _method;
  switch (method) {
    case 'boot':
      return boot() as T;
    case 'testConnection':
      return {
        state,
        latencyMs: state === 'online' ? 12 : null,
        daemons: state === 'online' ? daemons : [],
      } as T;
    case 'setConfig': {
      const p = params ?? {};
      if (typeof p.serverUrl === 'string') serverUrl = p.serverUrl;
      if (typeof p.autoStart === 'boolean') autoStart = p.autoStart;
      if (p.language === 'zh' || p.language === 'en') language = p.language;
      return { ok: true, ...boot() } as T;
    }
    case 'startServer':
      await delay(900);
      state = 'online';
      return boot() as T;
    case 'stopServer':
      await delay(600);
      state = 'offline';
      return boot() as T;
    case 'refresh':
      return boot() as T;
    case 'addDaemon': {
      const p = (params ?? {}) as { name?: string; url?: string; capabilities?: string };
      const name = p.name ?? 'new-daemon';
      const url = p.url ?? 'ws://192.168.1.30:8422';
      const caps = p.capabilities?.trim();
      const entry = caps ? `${name} ${url} ${caps}` : `${name} ${url}`;
      return {
        ok: true,
        daemonsPath: '/home/you/.agt/daemons',
        command: [
          'mkdir -p ~/.agt',
          `echo '${entry}' >> ~/.agt/daemons`,
          `AGT_LISTEN_MODE=1 AGT_SERVER_URL=${serverUrl} agt daemon start --detach`,
        ].join('\n'),
      } as T;
    }
    default:
      throw new Error(`mock: unknown method ${method}`);
  }
}
