import type {
  ChatSessionInfo,
  DaemonInfo,
  IdentityWithBindings,
  RuntimeInfo,
  ServerState,
  UiLanguage,
} from '../core/types';

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
let language: UiLanguage = 'zh';
let serverUrl = 'http://127.0.0.1:8420';
let autoStart = true;
let autoStartDaemon = true;
let daemonRunning = true;
const startCommand = 'uv run agt server start --detach';
const stopCommand = 'uv run agt server stop';
const daemonStartCommand = 'uv run agt daemon start --detach';
const daemonStopCommand = 'uv run agt daemon stop';

const runtimes: RuntimeInfo[] = [
  { name: 'Claude Code', type: 'claude', version: '2.1.266', status: 'online', device_name: 'local', daemon_id: 'dev-box-1', daemon_name: '本地开发机' },
  { name: 'Codex CLI', type: 'codex', version: '0.149.0', status: 'online', device_name: 'local', daemon_id: 'dev-box-1', daemon_name: '本地开发机' },
  { name: 'Codex CLI', type: 'codex', version: '0.147.0', status: 'online', device_name: '192.168.1.24', daemon_id: 'dev-box-2', daemon_name: null },
  { name: 'Kimi CLI', type: 'kimi', version: '1.2.3', status: 'online', device_name: '192.168.1.24', daemon_id: 'dev-box-2', daemon_name: null },
];

const identities: IdentityWithBindings[] = [
  {
    did: 'did:agt:code-reviewer',
    daemon_id: 'dev-box-1',
    name: 'Code Reviewer',
    description: 'Review code carefully',
    capabilities: ['code-review'],
    status: 'active',
    visibility: 'private',
    created_by: 'user',
    created_at: '2026-09-01 10:00:00',
    updated_at: '2026-09-01 10:00:00',
    bindings: [
      { id: 1, identity_did: 'did:agt:code-reviewer', daemon_id: 'dev-box-1', provider: 'claude', priority: 0, is_default: true, created_at: '2026-09-01 10:00:00' },
      { id: 2, identity_did: 'did:agt:code-reviewer', daemon_id: 'dev-box-1', provider: 'codex', priority: 0, is_default: false, created_at: '2026-09-01 10:00:00' },
    ],
  },
];

const sessionsByDaemon: Record<string, ChatSessionInfo[]> = {
  'dev-box-1': [
    { session_id: '11111111-1111-4111-8111-111111111111', task_id: 'mock001', provider: 'claude', identity_did: 'did:agt:code-reviewer', dir: '/home/you/project', first_prompt: 'Review the current diff', last_seen_at: '2026-09-15 10:00:00' },
    { session_id: '22222222-2222-4222-8222-222222222222', task_id: 'mock002', provider: 'codex', identity_did: null, dir: '/home/you/project', first_prompt: 'Summarize this repository', last_seen_at: '2026-09-14 18:20:00' },
  ],
  'dev-box-2': [
    { session_id: '33333333-3333-4333-8333-333333333333', task_id: 'mock003', provider: 'kimi', identity_did: null, dir: '/remote/project', first_prompt: 'Check Rust build', last_seen_at: '2026-09-15 09:20:00' },
  ],
};

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function boot(): Record<string, unknown> {
  return {
    serverUrl,
    autoStart,
    startCommand,
    stopCommand,
    autoStartDaemon,
    daemonStartCommand,
    daemonStopCommand,
    daemonRunning,
    language,
    state,
    latencyMs: state === 'online' ? 12 : null,
    daemons: state === 'online' ? daemons : [],
    runtimes: state === 'online' ? runtimes : [],
    identities: state === 'online' ? identities : [],
    sessions: [],
    workspaceFolders: ['/home/you/project'],
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
      if (typeof p.autoStartDaemon === 'boolean') autoStartDaemon = p.autoStartDaemon;
      if (p.language === 'zh' || p.language === 'en') language = p.language;
      return { ok: true, ...boot() } as T;
    }
    case 'startServer':
      await delay(900);
      state = 'online';
      if (autoStartDaemon) {
        daemonRunning = true;
      }
      return boot() as T;
    case 'stopServer':
      await delay(600);
      state = 'offline';
      return boot() as T;
    case 'startDaemon':
      await delay(700);
      daemonRunning = true;
      return boot() as T;
    case 'stopDaemon':
      await delay(500);
      daemonRunning = false;
      return boot() as T;
    case 'refresh':
      return boot() as T;
    case 'setLanguage':
      if (params?.language === 'zh' || params?.language === 'en') {
        language = params.language;
      }
      return { ok: true } as T;
    case 'openSettings':
      return { ok: true } as T;
    case 'sessions': {
      const daemonId = String(params?.daemonId ?? '');
      return { sessions: sessionsByDaemon[daemonId] ?? [] } as T;
    }
    case 'selectSession': {
      const sessionId = String(params?.sessionId ?? '');
      const found = Object.values(sessionsByDaemon)
        .flat()
        .find((session) => session.session_id === sessionId);
      if (!found) throw new Error('mock: session not found');
      return {
        resolved: {
          job_id: `job-${found.task_id}`,
          task_id: found.task_id,
          session_id: found.session_id,
          provider: found.provider,
          identity_did: found.identity_did,
          daemon_id: Object.entries(sessionsByDaemon).find(([, list]) =>
            list.some((session) => session.session_id === sessionId),
          )?.[0] ?? 'dev-box-1',
          dir: found.dir,
        },
        jobStatus: { status: 'waiting_input' },
        history: {
          session_id: found.session_id,
          not_implemented: false,
          messages: [
            { role: 'user', content: found.first_prompt },
            { role: 'assistant', content: 'This is a mocked historical response.' },
          ],
        },
      } as T;
    }
    case 'send': {
      const p = (params ?? {}) as { prompt?: string; currentJobId?: string | null };
      const jobId = p.currentJobId ?? `job-${Math.random().toString(16).slice(2, 8)}`;
      const taskId = `task-${Math.random().toString(16).slice(2, 8)}`;
      const answer = `Mock response\n\nYou said: ${p.prompt ?? ''}`;
      return {
        accepted: true,
        events: [
          { kind: 'started', job_id: jobId, task_id: taskId, status: 'pending' },
          { kind: 'snapshot', type: 'snapshot', status: 'pending', stdout: '' },
          { kind: 'delta', type: 'delta', chunk: answer.slice(0, Math.ceil(answer.length / 2)) },
          { kind: 'delta', type: 'delta', chunk: answer.slice(Math.ceil(answer.length / 2)) },
          {
            kind: 'done',
            type: 'done',
            status: 'completed',
            stdout: answer,
            stderr: '',
            session_id: `mock-${Math.random().toString(16).slice(2, 10)}`,
            exit_code: 0,
          },
          {
            kind: 'settled',
            jobStatus: { job_id: jobId, status: 'waiting_input', current_step_id: taskId },
            sessions: sessionsByDaemon['dev-box-1'] ?? [],
          },
        ],
      } as T;
    }
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
