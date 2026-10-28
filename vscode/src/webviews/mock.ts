import type {
  ChatSessionInfo,
  ConsoleProjectInfo,
  DaemonConfigInfo,
  DaemonInfo,
  GoalRunInfo,
  IdentityWithBindings,
  RunInfo,
  RuntimeInfo,
  ServerState,
  UiLanguage,
} from '../core/types';
import { applyIntentToModel } from '../core/sop/model-ops';
import type { SopPoint } from '../core/sop/layout';
import { issues as sopIssues } from '../core/sop/graph';
import type { EditIntent, SopDoc } from '../core/sop/model';

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

let consoleProjects: ConsoleProjectInfo[] = [
  {
    id: 'project-agent-team',
    name: 'agent-team',
    path: '/home/you/project',
    available: true,
    created_at: '2026-10-20T09:00:00Z',
    updated_at: '2026-10-20T09:00:00Z',
    last_opened_at: '2026-10-25T10:00:00Z',
    run_counts: { chat: 4, sop: 1, goal: 3 },
  },
  {
    id: 'project-docs',
    name: 'documentation',
    path: '/home/you/documentation',
    available: true,
    created_at: '2026-10-18T09:00:00Z',
    updated_at: '2026-10-18T09:00:00Z',
    last_opened_at: null,
    run_counts: { chat: 0, sop: 0, goal: 0 },
  },
];

let daemonConfigInfo: DaemonConfigInfo = {
  config_file: '/home/you/.agt/daemon.json',
  settings: {
    max_concurrent: {
      value: 4,
      source: 'env',
      saved: 2,
      default: 1,
      env_name: 'AGT_MAX_CONCURRENT',
    },
    agent_timeout: {
      value: 1800,
      source: 'file',
      saved: 1800,
      default: 600,
      env_name: 'AGT_AGENT_TIMEOUT',
    },
    work_dir: {
      value: '/home/you/.agt/workspaces',
      source: 'default',
      saved: null,
      default: '/home/you/.agt/workspaces',
      env_name: 'AGT_WORK_DIR',
    },
    agent_cwd_allowlist: {
      value: ['/home/you/project', '/data/repos'],
      source: 'file',
      saved: ['/home/you/project', '/data/repos'],
      default: ['/home/you'],
      env_name: 'AGT_AGENT_CWD_ALLOWLIST',
    },
  },
};

const runtimes: RuntimeInfo[] = [
  { name: 'Claude Code', type: 'claude', version: '2.1.266', status: 'online', device_name: 'local', daemon_id: 'dev-box-1', daemon_name: '本地开发机' },
  { name: 'Codex CLI', type: 'codex', version: '0.149.0', status: 'online', device_name: 'local', daemon_id: 'dev-box-1', daemon_name: '本地开发机' },
  { name: 'Codex CLI', type: 'codex', version: '0.147.0', status: 'online', device_name: '192.168.1.24', daemon_id: 'dev-box-2', daemon_name: null },
  { name: 'Kimi CLI', type: 'kimi', version: '1.2.3', status: 'online', device_name: '192.168.1.24', daemon_id: 'dev-box-2', daemon_name: null },
];

let identities: IdentityWithBindings[] = [
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
      { id: 1, identity_did: 'did:agt:code-reviewer', daemon_id: 'dev-box-1', provider: 'claude', priority: 0, is_default: true, created_at: '2026-09-01 10:00:00', runtime_online: true, last_seen: '2026-09-15 10:00:01' },
      { id: 2, identity_did: 'did:agt:code-reviewer', daemon_id: 'dev-box-1', provider: 'codex', priority: 10, is_default: false, created_at: '2026-09-01 10:00:00', runtime_online: true, last_seen: '2026-09-15 10:00:01' },
    ],
  },
  {
    did: 'did:agt:release-manager',
    daemon_id: 'dev-box-2',
    name: 'Release Manager',
    description: 'Coordinates release checks and changelog preparation.',
    capabilities: ['release', 'verification'],
    status: 'active',
    visibility: 'private',
    created_by: 'user',
    created_at: '2026-09-02 10:00:00',
    updated_at: '2026-09-02 10:00:00',
    bindings: [],
  },
];

const chatRuns: RunInfo[] = [
  {
    root_job_id: 'job-chat-001',
    mode: 'chat',
    status: 'waiting_input',
    provider: 'claude',
    identity_did: 'did:agt:code-reviewer',
    daemon_id: 'dev-box-1',
    cwd: '/home/you/project',
    template_path: null,
    template_hash: null,
    title: 'Review the current diff',
    created_at: '2026-09-15 09:55:00',
    updated_at: '2026-09-15 10:00:00',
  },
  {
    root_job_id: 'job-chat-002',
    mode: 'chat',
    status: 'waiting_input',
    provider: 'codex',
    identity_did: null,
    daemon_id: 'dev-box-1',
    cwd: '/home/you/project',
    template_path: null,
    template_hash: null,
    title: 'Summarize this repository',
    created_at: '2026-09-14 18:15:00',
    updated_at: '2026-09-14 18:20:00',
  },
  {
    root_job_id: 'job-chat-003',
    mode: 'chat',
    status: 'waiting_input',
    provider: 'kimi',
    identity_did: null,
    daemon_id: 'dev-box-2',
    cwd: '/remote/project',
    template_path: null,
    template_hash: null,
    title: 'Check Rust build',
    created_at: '2026-09-15 09:15:00',
    updated_at: '2026-09-15 09:20:00',
  },
];

function toChatSessionInfo(run: RunInfo): ChatSessionInfo {
  return {
    session_id: `sess-${run.root_job_id}`,
    task_id: `task-${run.root_job_id}`,
    job_id: run.root_job_id,
    provider: run.provider,
    identity_did: run.identity_did,
    dir: run.cwd,
    first_prompt: run.title ?? run.root_job_id,
    last_seen_at: run.updated_at,
  };
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/* ── SOP editor fixture ────────────────────────────────────────────── */

const sopTemplates = [
  { path: '/home/you/project/pipelines/review-fix.sop.yaml', label: 'review-fix.sop.yaml', nodeCount: 3 },
  { path: '/home/you/project/pipelines/release.sop.yaml', label: 'release.sop.yaml', nodeCount: 2 },
];

let sopPath: string | null = sopTemplates[0].path;
let sopDirty = false;
let sopValidation: { ok: boolean; error: string | null; at: number } | null = null;
let sopPositions: Record<string, SopPoint> = {};
let sopRun: { rootJobId: string; startedAt: number; stale: boolean } | null = null;
let sopRunTick = 0;

const sopDocs: Record<string, SopDoc> = {
  '/home/you/project/pipelines/review-fix.sop.yaml': {
    name: 'review-fix',
    identities: [
      { alias: 'reviewer', did: 'did:agt:reviewer', provider: 'claude' },
      { alias: 'tester', did: '', provider: 'codex' },
    ],
    nodes: [
      {
        name: 'review',
        identity: 'reviewer',
        dependsOn: [],
        input: '审查当前 diff，输出审查报告',
        output: 'review-report',
        provider: '',
        daemon: '',
        dir: '',
        index: 0,
      },
      {
        name: 'fix',
        identity: 'reviewer',
        dependsOn: ['review'],
        input: '根据 ${review.review-report} 修复问题',
        output: 'fix-result',
        provider: '',
        daemon: '',
        dir: '',
        index: 1,
      },
      {
        name: 'test',
        identity: 'tester',
        dependsOn: ['fix'],
        input: '为 ${fix.fix-result} 补单测并运行',
        output: '',
        provider: '',
        daemon: '',
        dir: '',
        index: 2,
      },
    ],
    loops: [],
  },
  '/home/you/project/pipelines/release.sop.yaml': {
    name: 'release',
    identities: [{ alias: 'packager', did: '', provider: 'codex' }],
    nodes: [
      {
        name: 'bump',
        identity: 'packager',
        dependsOn: [],
        input: '升级版本号并写 CHANGELOG',
        output: 'changelog',
        provider: '',
        daemon: '',
        dir: '',
        index: 0,
      },
      {
        name: 'publish',
        identity: 'packager',
        dependsOn: ['bump'],
        input: '基于 ${bump.changelog} 打 tag 并发布',
        output: '',
        provider: '',
        daemon: '',
        dir: '',
        index: 1,
      },
    ],
    loops: [],
  },
};

/** Dev-preview only: a readable YAML echo of the model (no round-trip). */
function renderSopYaml(doc: SopDoc): string {
  const lines = [`name: ${doc.name}`];
  if (doc.identities.length > 0) {
    lines.push('identities:');
    for (const identity of doc.identities) {
      const parts: string[] = [];
      if (identity.did) parts.push(`did: ${identity.did}`);
      if (identity.provider) parts.push(`provider: ${identity.provider}`);
      lines.push(`  ${identity.alias}: {${parts.join(', ')}}`);
    }
  }
  if (doc.loops.length > 0) {
    lines.push('loops:');
    for (const loop of doc.loops) {
      lines.push(`  - name: ${loop.name}`);
      lines.push(`    nodes: [${loop.nodes.join(', ')}]`);
      if (loop.entry) lines.push(`    entry: ${loop.entry}`);
      lines.push(`    max_iterations: ${loop.maxIterations}`);
    }
  }
  lines.push('tasks:');
  for (const node of doc.nodes) {
    lines.push(`  - name: ${node.name}`);
    if (node.identity) lines.push(`    identity: ${node.identity}`);
    if (node.input) lines.push(`    input: ${node.input}`);
    if (node.output) lines.push(`    output: ${node.output}`);
    if (node.provider) lines.push(`    provider: ${node.provider}`);
    if (node.daemon) lines.push(`    daemon: ${node.daemon}`);
    if (node.dir) lines.push(`    dir: ${node.dir}`);
    if (node.dependsOn.length > 0) lines.push(`    depends_on: [${node.dependsOn.join(', ')}]`);
  }
  return `${lines.join('\n')}\n`;
}

function toSopRunSummary(
  run: typeof sopRun,
  overrides?: Partial<Record<string, unknown>>,
): Record<string, unknown> | null {
  if (!run) return null;
  return {
    rootJobId: run.rootJobId,
    name: run.rootJobId,
    status: 'running',
    provider: 'claude',
    daemonId: 'dev-box-1',
    updatedAt: new Date(run.startedAt).toISOString(),
    startedAt: run.startedAt,
    stale: run.stale,
    templatePath: sopPath,
    ...overrides,
  };
}

function sopBoot(): Record<string, unknown> {
  const doc = sopPath ? sopDocs[sopPath] : null;
  const currentRun = toSopRunSummary(sopRun);
  const serverRuns: Record<string, unknown>[] =
    sopPath === '/home/you/project/pipelines/review-fix.sop.yaml'
      ? [
          {
            rootJobId: 'job-sop-001',
            name: 'review-fix',
            status: 'done',
            provider: 'claude',
            daemonId: 'dev-box-1',
            updatedAt: '2026-09-15T10:00:00.000Z',
            startedAt: Date.parse('2026-09-15T09:55:00.000Z'),
            stale: false,
            templatePath: sopPath,
          },
        ]
      : [];
  const runs: Record<string, unknown>[] = currentRun
    ? [currentRun, ...serverRuns]
    : serverRuns;
  return {
    language,
    state,
    templatePath: sopPath,
    templates: sopTemplates,
    daemons: daemons.map((daemon) => ({
      id: daemon.daemon_id,
      label: daemon.daemon_name || daemon.device_name || daemon.daemon_id,
      online: daemon.online,
      providers: [...new Set(daemon.providers.map((p) => p.provider))].sort(),
    })),
    model: doc,
    identities: identities
      .filter((identity) => identity.status === 'active')
      .map((identity) => ({
        did: identity.did,
        name: identity.name,
        status: identity.status,
        bindings: identity.bindings,
      })),
    text: doc ? renderSopYaml(doc) : '',
    dirty: sopDirty,
    positions: sopPath ? { ...sopPositions } : {},
    parseError: null,
    workspaceFolder: '/home/you/project',
    localDaemonId: 'dev-box-1',
    sidebarWidth: null,
    inspectorWidth: null,
    trackedRun: currentRun,
    runs,
    run: currentRun,
  };
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
    localDaemonId: 'dev-box-1',
    runtimes: state === 'online' ? runtimes : [],
    identities: state === 'online' ? identities : [],
    sessions: [],
    workspaceFolders: ['/home/you/project'],
    workspaceRoot: '/home/you/project',
  };
}

const goalArtifacts = [
  {
    artifact_id: 'goal-artifact-1',
    job_id: 'goal-child-1',
    step_id: 'goal-child-step',
    producer_identity_id: null,
    name: 'review-report.md',
    mime_type: 'text/markdown',
    summary: null,
    status_flag: 'pass',
    size: 2048,
    sha256: 'mock',
    status: 'ready',
    uri: 'agt-artifact://goal-artifact-1',
    created_at: '2026-09-25 10:00:00',
  },
];

const goalNode = {
  job_id: 'goal-root',
  parent_job_id: null,
  status: 'waiting_children',
  provider: 'claude',
  identity_did: null,
  daemon_id: 'dev-box-1',
  session_id: 'goal-root-session',
  label: 'Orchestrate deployment guide',
  duration: 42,
  current_task_id: 'goal-root-task',
  ask_question: null,
  artifacts: [],
  error: null,
  children: [
    {
      job_id: 'goal-child-1',
      parent_job_id: 'goal-root',
      status: 'done',
      provider: 'claude',
      identity_did: 'did:agt:reviewer',
      daemon_id: 'dev-box-1',
      session_id: 'goal-child-session',
      label: 'Review repository',
      duration: 28,
      current_task_id: 'goal-child-task',
      ask_question: null,
      artifacts: goalArtifacts,
      error: null,
      children: [],
    },
    {
      job_id: 'goal-child-2',
      parent_job_id: 'goal-root',
      status: 'running',
      provider: 'codex',
      identity_did: null,
      daemon_id: 'dev-box-2',
      session_id: null,
      label: 'Draft deployment guide',
      duration: 16,
      current_task_id: 'goal-child-2-task',
      ask_question: null,
      artifacts: [],
      error: null,
      children: [],
    },
  ],
};

const goalRun: GoalRunInfo = {
  root_job_id: 'goal-root',
  goal: 'Write a deployment guide for the current repository',
  status: 'waiting_children',
  provider: 'claude',
  identity_did: null,
  daemon_id: 'dev-box-1',
  cwd: '/home/you/project',
  created_at: '2026-09-25 09:58:00',
  updated_at: '2026-09-25 10:01:00',
  total_nodes: 3,
  status_counts: { waiting_children: 1, done: 1, running: 1 },
  meta: {
    goal: 'Write a deployment guide for the current repository',
    template: 'builtin',
    budget: { max_depth: 5, max_descendants: 64, max_batch: 8 },
    goal_sha256: 'mock-goal-sha',
  },
};

const goalLogs = new Map<string, unknown>([
  ['goal-root', {
    job_id: 'goal-root',
    status: 'waiting_children',
    steps: [{ task_id: 'goal-root-task', status: 'completed', session_id: 'goal-root-session', exit_code: 0, stdout: 'I will split this into review and writing tasks.', stderr: '' }],
  }],
  ['goal-child-1', {
    job_id: 'goal-child-1',
    status: 'done',
    steps: [{ task_id: 'goal-child-task', status: 'completed', session_id: 'goal-child-session', exit_code: 0, stdout: 'Review completed. Three sections need examples.', stderr: '' }],
  }],
  ['goal-child-2', {
    job_id: 'goal-child-2',
    status: 'running',
    steps: [],
  }],
]);

function goalBoot(): Record<string, unknown> {
  return {
    language,
    state,
    runs: [goalRun],
    selectedRunId: 'goal-root',
    tree: { root_job_id: 'goal-root', goal: goalRun.meta, node: goalNode },
    selectedNodeId: 'goal-child-1',
    identities: state === 'online' ? identities : [],
    daemons: state === 'online' ? daemons : [],
    workspaceFolder: '/home/you/project',
    inspectorWidth: null,
    sidebarWidth: 302,
    sidebarCollapsed: false,
  };
}

export async function mockCall<T>(_method: string, params?: Record<string, unknown>): Promise<T> {
  await delay(200 + Math.random() * 200);
  const method = _method;
  switch (method) {
    case 'goal.boot':
      return goalBoot() as T;
    case 'goal.select':
      return goalBoot() as T;
    case 'goal.tree':
      return { root_job_id: 'goal-root', goal: goalRun.meta, node: goalNode } as T;
    case 'goal.nodeLog':
      return (goalLogs.get(String(params?.jobId ?? '')) ?? { job_id: params?.jobId, status: 'running', steps: [] }) as T;
    case 'goal.taskStatus': {
      const taskId = String(params?.taskId ?? '');
      return {
        task_id: taskId,
        provider: 'codex',
        status: 'running',
        session_id: null,
        exit_code: null,
        stdout: 'Writing the deployment guide...\n- installation\n- configuration\n',
        stderr: '',
        duration: null,
        created_at: '2026-09-25 10:00:00',
        updated_at: '2026-09-25 10:01:00',
      } as T;
    }
    case 'goal.submit':
      return { root_job_id: 'goal-root', status: 'queued', ...goalBoot() } as T;
    case 'goal.answer':
      return goalBoot() as T;
    case 'goal.cancel':
      return { cancelled: true } as T;
    case 'goal.export':
      return { canceled: false, path: '/home/you/project/goal-root.sop.yaml', warnings: [] } as T;
    case 'goal.setSidebarWidth':
      return { ok: true, width: params?.width } as T;
    case 'goal.setSidebarCollapsed':
      return { ok: true, collapsed: params?.collapsed } as T;
    case 'openGoal':
      return { ok: true } as T;
    case 'sop.boot':
      return sopBoot() as T;
    case 'sop.openTemplate': {
      const path = String(params?.path ?? '');
      if (sopDocs[path]) {
        sopPath = path;
        sopPositions = {};
        sopDirty = false;
        sopValidation = null;
      }
      return sopBoot() as T;
    }
    case 'sop.newSop': {
      const path = `/home/you/project/pipelines/untitled-${Object.keys(sopDocs).length + 1}.sop.yaml`;
      sopDocs[path] = {
        name: 'untitled',
        identities: [],
        nodes: [
          {
            name: 'task',
            identity: '',
            dependsOn: [],
            input: '',
            output: '',
            provider: '',
            daemon: '',
            dir: '',
            index: 0,
          },
        ],
        loops: [],
      };
      sopTemplates.push({ path, label: path.split('/').pop() ?? path, nodeCount: 1 });
      sopPath = path;
      sopPositions = {};
      sopDirty = true;
      return sopBoot() as T;
    }
    case 'sop.apply': {
      const doc = sopPath ? sopDocs[sopPath] : null;
      const intent = params?.intent as EditIntent | undefined;
      if (doc && intent) {
        sopDocs[sopPath as string] = applyIntentToModel(doc, intent);
        sopDirty = true;
        sopValidation = null;
        if (sopRun) {
          sopRun = { ...sopRun, stale: true };
        }
        const template = sopTemplates.find((t) => t.path === sopPath);
        if (template) {
          template.nodeCount = sopDocs[sopPath as string].nodes.length;
        }
      }
      return sopBoot() as T;
    }
    case 'sop.renameFile': {
      const source = String(params?.path ?? '');
      const newName = String(params?.newName ?? '').trim();
      if (!source || !newName || newName.includes('/')) {
        throw new Error('invalid SOP filename');
      }
      const destination = `${source.slice(0, source.lastIndexOf('/') + 1)}${newName}`;
      const template = sopTemplates.find((item) => item.path === source);
      if (!template) throw new Error('SOP file not found');
      if (sopTemplates.some((item) => item.path === destination)) {
        throw new Error('SOP filename already exists');
      }
      template.path = destination;
      template.label = newName;
      if (sopDocs[source]) {
        sopDocs[destination] = sopDocs[source];
        delete sopDocs[source];
      }
      if (sopPath === source) sopPath = destination;
      return sopBoot() as T;
    }
    case 'sop.save':
      sopDirty = false;
      return sopBoot() as T;
    case 'sop.validate': {
      const doc = sopPath ? sopDocs[sopPath] : null;
      const problems = doc ? sopIssues(doc).filter((i) => i.level === 'error') : [];
      sopValidation = problems.length > 0
        ? { ok: false, error: problems[0].message, at: Date.now() }
        : { ok: true, error: null, at: Date.now() };
      return sopValidation as T;
    }
    case 'sop.revealYaml':
      return { ok: true } as T;
    case 'sop.restartServer':
      await delay(600);
      return { ok: true } as T;
    case 'sop.run': {
      await delay(500);
      if (!sopPath) throw new Error('no-document');
      const doc = sopDocs[sopPath];
      sopRunTick = 0;
      sopRun = {
        rootJobId: `root-${Math.random().toString(16).slice(2, 10)}`,
        startedAt: Date.now(),
        stale: false,
      };
      return {
        rootJobId: sopRun.rootJobId,
        nodes: Object.fromEntries(doc.nodes.map((n, i) => [n.name, `job-${i}`])),
        ...sopBoot(),
      } as T;
    }
    case 'sop.status': {
      // The mock advances one node per poll so the canvas really animates.
      if (!sopRun || !sopPath) throw new Error('no-run');
      const doc = sopDocs[sopPath];
      sopRunTick += 1;
      const terminal = Math.floor(sopRunTick / 1);
      const nodes = doc.nodes.map((node, index) => ({
        name: node.name,
        job_id: `job-${index}`,
        status:
          index < terminal - 1
            ? index === doc.nodes.length - 1
              ? 'done'
              : index === 1
                ? 'failed'
                : 'done'
            : index === terminal - 1
              ? 'running'
              : 'blocked',
        depends_on: node.dependsOn,
        artifacts:
          index === 0 && terminal > 0 ? ['agt-artifact://mock-review-report'] : [],
        provider: node.identity || 'claude',
        session_id: index < terminal - 1 ? `sess-${index}` : null,
      }));
      return {
        root_job_id: sopRun.rootJobId,
        nodes,
        all_terminal: terminal > doc.nodes.length,
      } as T;
    }
    case 'sop.nodeLog': {
      const name = String(params?.node ?? '');
      const status = 'done';
      return {
        node_name: name,
        job_id: `job-${name}`,
        status,
        provider: 'claude',
        steps: [
          {
            task_id: `task-${name}-1`,
            status,
            session_id: `sess-${name}`,
            exit_code: 0,
            stdout: `[mock] ${name} 执行完成\n产物已写入 .agt/outputs/`,
            stderr: null,
          },
        ],
      } as T;
    }
    case 'sop.cancelRun':
      return { cancelled: ['mock-node'] } as T;
    case 'sop.forgetRun':
      sopRun = null;
      return sopBoot() as T;
    case 'sop.setLayout': {
      sopPositions = (params?.positions as Record<string, SopPoint>) ?? {};
      return { ok: true } as T;
    }
    case 'sop.setSidebarWidth':
      return { ok: true, width: params?.width } as T;
    case 'openChat':
    case 'openSop':
      return { ok: true } as T;
    case 'boot':
      return boot() as T;
    case 'projects.boot':
      return {
        language,
        state,
        projects: consoleProjects,
      } as T;
    case 'projects.directories':
      return {
        roots: ['/home/you'],
        parent: '/home/you',
        current: String(params?.path ?? '/home/you/project'),
        directories: [
          { name: 'project', path: '/home/you/project' },
          { name: 'documentation', path: '/home/you/documentation' },
          { name: 'sandbox', path: '/home/you/sandbox' },
        ],
      } as T;
    case 'projects.pickDirectory':
      return { path: '/home/you/project' } as T;
    case 'projects.create': {
      const path = String(params?.path ?? '');
      if (!path) throw new Error('project path is required');
      const id = `project-${Math.random().toString(16).slice(2, 10)}`;
      const now = new Date().toISOString();
      const project: ConsoleProjectInfo = {
        id,
        name: String(params?.name || path.split('/').filter(Boolean).pop() || 'Project'),
        path,
        available: true,
        created_at: now,
        updated_at: now,
        last_opened_at: null,
        run_counts: { chat: 0, sop: 0, goal: 0 },
      };
      consoleProjects = [project, ...consoleProjects];
      return { project, projects: consoleProjects } as T;
    }
    case 'projects.rename': {
      const projectId = String(params?.projectId ?? '');
      consoleProjects = consoleProjects.map((project) =>
        project.id === projectId
          ? { ...project, name: String(params?.name ?? project.name) }
          : project,
      );
      return {
        project: consoleProjects.find((project) => project.id === projectId),
        projects: consoleProjects,
      } as T;
    }
    case 'projects.delete':
      consoleProjects = consoleProjects.filter((project) => project.id !== params?.projectId);
      return { projects: consoleProjects } as T;
    case 'projects.open':
      return consoleProjects.find((project) => project.id === params?.projectId) as T;
    case 'openProjects':
      return { ok: true } as T;
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
    case 'identityCreate': {
      const p = params ?? {};
      const name = String(p.name ?? '').trim();
      const slug = String(
        p.slug
          || name
            .toLowerCase()
            .replace(/[^a-z0-9]+/g, '-')
            .replace(/^-+|-+$/g, ''),
      );
      const did = `did:agt:${slug}`;
      if (identities.some((identity) => identity.did === did)) {
        throw new Error('HTTP 409: Identity with this slug already exists');
      }
      const now = new Date().toISOString();
      const created: IdentityWithBindings = {
        did,
        daemon_id: 'dev-box-1',
        name,
        description: String(p.description ?? ''),
        capabilities: Array.isArray(p.capabilities)
          ? p.capabilities.map(String).filter(Boolean)
          : [],
        status: 'active',
        visibility: 'private',
        created_by: 'user',
        created_at: now,
        updated_at: now,
        bindings: [],
      };
      identities = [created, ...identities];
      return { created, identities } as T;
    }
    case 'identityUpdate': {
      const p = params ?? {};
      const did = String(p.did ?? '');
      const index = identities.findIndex((identity) => identity.did === did);
      if (index < 0) throw new Error('HTTP 404: identity not found');
      const current = identities[index];
      const updated: IdentityWithBindings = {
        ...current,
        name: String(p.name ?? current.name),
        description: String(p.description ?? current.description),
        capabilities: Array.isArray(p.capabilities)
          ? p.capabilities.map(String).filter(Boolean)
          : current.capabilities,
        updated_at: new Date().toISOString(),
      };
      identities = identities.map((identity) =>
        identity.did === did ? updated : identity,
      );
      return { updated, identities } as T;
    }
    case 'identityBindingCreate': {
      const p = params ?? {};
      const did = String(p.did ?? '');
      const index = identities.findIndex((identity) => identity.did === did);
      if (index < 0) throw new Error('HTTP 404: identity not found');
      const daemonId = String(p.daemonId ?? '');
      const provider = String(p.provider ?? '');
      if (identities[index].bindings.some(
        (binding) => binding.daemon_id === daemonId && binding.provider === provider,
      )) {
        throw new Error('HTTP 409: binding already exists');
      }
      const daemon = daemons.find((item) => item.daemon_id === daemonId);
      const runtime = daemon?.providers.find((item) => item.provider === provider);
      const bindingId = Math.max(
        0,
        ...identities.flatMap((identity) =>
          identity.bindings.map((binding) => binding.id)
        ),
      ) + 1;
      const identity = identities[index];
      const bindings = identity.bindings.map((binding) =>
        p.isDefault === true
          ? { ...binding, is_default: false }
          : binding,
      );
      bindings.push({
        id: bindingId,
        identity_did: did,
        daemon_id: daemonId,
        provider,
        priority: Number(p.priority ?? 0),
        is_default: p.isDefault === true,
        created_at: new Date().toISOString(),
        runtime_online: !!runtime,
        last_seen: runtime?.last_seen ?? null,
      });
      identities = identities.map((item, itemIndex) =>
        itemIndex === index ? { ...item, bindings } : item,
      );
      return { identities } as T;
    }
    case 'identityBindingUpdate': {
      const p = params ?? {};
      const did = String(p.did ?? '');
      const bindingId = Number(p.bindingId);
      const index = identities.findIndex((identity) => identity.did === did);
      if (index < 0) throw new Error('HTTP 404: identity not found');
      const identity = identities[index];
      const bindings = identity.bindings.map((binding) => {
        if (p.isDefault === true) {
          return binding.id === bindingId
            ? {
                ...binding,
                priority: p.priority === undefined ? binding.priority : Number(p.priority),
                is_default: true,
              }
            : { ...binding, is_default: false };
        }
        return binding.id === bindingId
          ? {
              ...binding,
              priority: p.priority === undefined ? binding.priority : Number(p.priority),
              is_default: p.isDefault === false ? false : binding.is_default,
            }
          : binding;
      });
      identities = identities.map((item, itemIndex) =>
        itemIndex === index ? { ...item, bindings } : item,
      );
      return { identities } as T;
    }
    case 'identityBindingDelete': {
      const did = String(params?.did ?? '');
      const bindingId = Number(params?.bindingId);
      identities = identities.map((identity) =>
        identity.did === did
          ? {
              ...identity,
              bindings: identity.bindings.filter((binding) => binding.id !== bindingId),
            }
          : identity,
      );
      return { identities } as T;
    }
    case 'daemonConfigShow': {
      const daemonId = String(params?.daemonId ?? '');
      if (daemonId !== 'dev-box-1') {
        throw new Error('remote-daemon-config-unsupported');
      }
      return daemonConfigInfo as T;
    }
    case 'daemonConfigSet': {
      const daemonId = String(params?.daemonId ?? '');
      if (daemonId !== 'dev-box-1') {
        throw new Error('remote-daemon-config-unsupported');
      }
      const p = params ?? {};
      if (typeof p.maxConcurrent === 'number') {
        daemonConfigInfo.settings.max_concurrent.saved = p.maxConcurrent;
        if (daemonConfigInfo.settings.max_concurrent.source !== 'env') {
          daemonConfigInfo.settings.max_concurrent.value = p.maxConcurrent;
        }
      }
      if (typeof p.agentTimeout === 'number') {
        daemonConfigInfo.settings.agent_timeout.saved = p.agentTimeout;
        if (daemonConfigInfo.settings.agent_timeout.source !== 'env') {
          daemonConfigInfo.settings.agent_timeout.value = p.agentTimeout;
        }
      }
      if (typeof p.workDir === 'string') {
        daemonConfigInfo.settings.work_dir.saved = p.workDir;
        if (daemonConfigInfo.settings.work_dir.source !== 'env') {
          daemonConfigInfo.settings.work_dir.value = p.workDir;
        }
      }
      if (Array.isArray(p.agentCwdAllowlist)) {
        const allowlist = p.agentCwdAllowlist.filter((item): item is string => typeof item === 'string');
        daemonConfigInfo.settings.agent_cwd_allowlist.saved = allowlist;
        if (daemonConfigInfo.settings.agent_cwd_allowlist.source !== 'env') {
          daemonConfigInfo.settings.agent_cwd_allowlist.value = allowlist;
        }
      }
      return daemonConfigInfo as T;
    }
    case 'sessions': {
      const daemonId = String(params?.daemonId ?? '');
      const sessions = chatRuns
        .filter((run) => run.daemon_id === daemonId)
        .map(toChatSessionInfo);
      return { sessions } as T;
    }
    case 'selectSession': {
      const jobId = String(params?.jobId ?? '');
      const found = chatRuns.find((run) => run.root_job_id === jobId);
      if (!found) throw new Error('mock: session not found');
      const sessionId = `sess-${found.root_job_id}`;
      return {
        resolved: {
          job_id: found.root_job_id,
          task_id: `task-${found.root_job_id}`,
          session_id: sessionId,
          provider: found.provider,
          identity_did: found.identity_did,
          daemon_id: found.daemon_id ?? 'dev-box-1',
          dir: found.cwd,
        },
        jobStatus: { status: 'waiting_input' },
        history: {
          session_id: sessionId,
          not_implemented: false,
          messages: [
            { role: 'user', content: found.title ?? found.root_job_id },
            { role: 'assistant', content: 'This is a mocked historical response.' },
          ],
        },
      } as T;
    }
    case 'stop': {
      const jobId = String(params?.jobId ?? '');
      if (!jobId) throw new Error('mock: no active chat job');
      return {
        job_id: jobId,
        status: 'waiting_input',
        interrupted_task_ids: [`task-${jobId}`],
      } as T;
    }
    case 'deleteSession': {
      const jobId = String(params?.jobId ?? '');
      const daemonId = String(params?.daemonId ?? '');
      const index = chatRuns.findIndex(
        (run) => run.root_job_id === jobId && run.daemon_id === daemonId,
      );
      if (index < 0) throw new Error('mock: session not found');
      chatRuns.splice(index, 1);
      const sessions = chatRuns
        .filter((run) => run.daemon_id === daemonId)
        .map(toChatSessionInfo);
      return {
        deleted: {
          ok: true,
          session_id: `sess-${jobId}`,
          session_rows_deleted: 1,
          task_pointers_cleared: 1,
        },
        sessions,
      } as T;
    }
    case 'upload': {
      const p = (params ?? {}) as { name?: string; mimeType?: string; contentB64?: string };
      if (!p.contentB64) throw new Error('upload content is required');
      const uploadId = `mock-${Math.random().toString(16).slice(2, 10)}`;
      // base64 length → approximate byte size, enough for the mock chips.
      const size = Math.floor((p.contentB64.length * 3) / 4);
      return {
        upload_id: uploadId,
        name: p.name ?? 'attachment.bin',
        mime_type: p.mimeType ?? 'application/octet-stream',
        size,
        sha256: '0'.repeat(64),
        uri: `agt-upload://${uploadId}`,
        source: 'mock',
        created_at: new Date().toISOString(),
        expires_at: null,
      } as T;
    }
    case 'deleteUpload':
      return { ok: true } as T;
    case 'send': {
      const p = (params ?? {}) as {
        prompt?: string;
        currentJobId?: string | null;
        daemonId?: string;
        message?: { parts?: { type?: string; name?: string }[] };
      };
      const daemonId = p.daemonId ?? 'dev-box-1';
      const jobId = p.currentJobId ?? `job-${Math.random().toString(16).slice(2, 8)}`;
      const taskId = `task-${Math.random().toString(16).slice(2, 8)}`;
      const files = (p.message?.parts ?? [])
        .filter((part) => part.type === 'file')
        .map((part) => part.name ?? 'attachment');
      const fileNote = files.length ? `\n\n(${files.length} attachment(s): ${files.join(', ')})` : '';
      const answer = `Mock response\n\nYou said: ${p.prompt ?? ''}${fileNote}`;
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
            sessions: chatRuns
              .filter((run) => run.daemon_id === daemonId)
              .map(toChatSessionInfo),
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
