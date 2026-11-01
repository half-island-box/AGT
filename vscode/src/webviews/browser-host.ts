/**
 * Browser host for the packaged AGT console.
 *
 * This adapts the existing VSCode webview message contract to same-origin
 * HTTP and SSE APIs. It must not import `vscode` or Node-only modules that
 * are unavailable in a browser.
 */

import { AgtApi } from '../core/api';
import { agentOptionsFromDaemons, isLocalDaemon } from '../core/daemons';
import type {
  ChatInputMessage,
  ChatSessionInfo,
  ConsoleDirectoryListing,
  ConsoleProjectInfo,
  DaemonInfo,
  GoalRunInfo,
  IdentityWithBindings,
  RunInfo,
  SessionHistoryInfo,
  TaskStatusInfo,
  UiLanguage,
} from '../core/types';
import type { EditIntent } from '../core/sop/model';
import type { SopPoint as LayoutPoint } from '../core/sop/layout';
import {
  applyIntent,
  parseSopText,
  serializeSop,
} from '../core/sop/yaml-doc';
import { fingerprint, localDaemonId } from '../core/sop/run';

type Params = Record<string, unknown>;
type Emit = (event: string, payload: unknown) => void;

const PROJECT_KEY = 'agt.console.project';
const LANGUAGE_KEY = 'agt.console.language';
const GOAL_RUN_KEY = 'agt.console.goal.selectedRun';
const GOAL_NODE_KEY = 'agt.console.goal.selectedNode';
const SOP_PATH_KEY = 'agt.console.sop.path';
const SOP_RUN_KEY = 'agt.console.sop.trackedRun';
const SOP_SELECTED_RUN_KEY = 'agt.console.sop.selectedRun';

const api = new AgtApi(window.location.origin);

function sopTrackedRunKey(templatePath: string): string {
  return `agt.console.sop.trackedRun:${templatePath}`;
}

function sopSelectedRunKey(templatePath: string): string {
  return `agt.console.sop.selectedRun:${templatePath}`;
}

function storageGet(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function storageSet(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch {
    // Private browsing or policy may disable storage; the in-memory page
    // remains usable.
  }
}

function routeProjectId(): string | null {
  const match = window.location.pathname.match(
    /^\/console\/projects\/([^/]+)(?:\/(?:chat|sop|goal))?$/,
  );
  if (match) return decodeURIComponent(match[1]);
  const returnTo = new URLSearchParams(window.location.search).get('returnTo');
  const returned = returnTo?.match(
    /^\/console\/projects\/([^/]+)(?:\/(?:chat|sop|goal))?$/,
  );
  return returned ? decodeURIComponent(returned[1]) : null;
}

function modeUrl(mode: 'chat' | 'sop' | 'goal'): string {
  const projectId = routeProjectId();
  return projectId
    ? `/console/projects/${encodeURIComponent(projectId)}/${mode}`
    : `/console/${mode}`;
}

async function activeProject(): Promise<ConsoleProjectInfo | null> {
  const projectId = routeProjectId();
  if (!projectId) return null;
  const project = await api.consoleProject(projectId);
  if (!project.available) throw new Error('project directory is unavailable');
  storageSet(PROJECT_KEY, project.path);
  return project;
}

function language(): UiLanguage {
  return storageGet(LANGUAGE_KEY) === 'en' ? 'en' : 'zh';
}

function projectPath(bootstrapDefault: string): string {
  return storageGet(PROJECT_KEY)?.trim() || bootstrapDefault;
}

/**
 * Working directory for a run dispatched to ``daemonId``.
 *
 * The project's local path only exists on the Server machine; when the
 * project also pins a directory on the selected remote daemon, that
 * directory is the default. ``null`` means the caller must pick one.
 */
function dirForDaemon(
  project: ConsoleProjectInfo | null,
  daemonId: string | null | undefined,
  localDaemonIdValue: string | null,
  fallback: string,
): string {
  if (
    project?.remote_daemon_id &&
    project.remote_path &&
    daemonId === project.remote_daemon_id
  ) {
    return project.remote_path;
  }
  if (!project) return fallback;
  if (!daemonId || daemonId === localDaemonIdValue) return project.path;
  // A remote daemon that is not the project's pinned one cannot see the
  // local project path; the caller must choose a directory on that machine.
  return '';
}

function preferredDaemon(daemons: DaemonInfo[]): DaemonInfo | undefined {
  const derived = localDaemonId(
    daemons.map((daemon) => ({ daemon_id: daemon.daemon_id, url: daemon.url })),
    window.location.hostname,
  );
  return daemons.find((daemon) => daemon.daemon_id === derived)
    ?? daemons.find(isLocalDaemon)
    ?? daemons.find((daemon) => daemon.online)
    ?? daemons[0];
}

function toChatSession(run: RunInfo): ChatSessionInfo {
  return {
    session_id: run.root_job_id,
    task_id: run.root_job_id,
    job_id: run.root_job_id,
    provider: run.provider,
    identity_did: run.identity_did,
    daemon_id: run.daemon_id,
    dir: run.cwd,
    first_prompt: run.title ?? run.root_job_id,
    last_seen_at: run.updated_at,
  };
}

function toGoalRun(run: RunInfo): GoalRunInfo {
  return {
    root_job_id: run.root_job_id,
    goal: run.title ?? run.root_job_id,
    status: run.status,
    provider: run.provider,
    identity_did: run.identity_did,
    daemon_id: run.daemon_id,
    cwd: run.cwd,
    created_at: run.created_at,
    updated_at: run.updated_at,
    total_nodes: 0,
    status_counts: {},
    meta: {
      goal: run.title ?? '',
      template: null,
      budget: null,
      goal_sha256: null,
    },
  };
}

function toRunSummary(run: RunInfo, text = ''): Record<string, unknown> {
  return {
    rootJobId: run.root_job_id,
    name: run.title ?? run.root_job_id,
    status: run.status,
    provider: run.provider,
    daemonId: run.daemon_id,
    updatedAt: run.updated_at,
    startedAt: Date.parse(run.created_at) || Date.now(),
    stale:
      run.template_hash !== null &&
      run.template_hash !== undefined &&
      run.template_hash !== fingerprint(text),
    templatePath: run.template_path,
    templateHash: run.template_hash,
  };
}

function download(name: string, bytes: BlobPart, mime = 'application/octet-stream'): void {
  const url = URL.createObjectURL(new Blob([bytes], { type: mime }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = name;
  anchor.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function pageKind(): 'chat' | 'settings' | 'sop' | 'goal' {
  const match = window.location.pathname.match(/^\/console\/(chat|settings|sop|goal)/);
  return (match?.[1] as 'chat' | 'settings' | 'sop' | 'goal' | undefined) ?? 'chat';
}

async function bootstrapContext(): Promise<{
  bootstrap: Awaited<ReturnType<AgtApi['consoleBootstrap']>>;
  daemons: DaemonInfo[];
  identities: IdentityWithBindings[];
}> {
  const [bootstrap, daemons, identities] = await Promise.all([
    api.consoleBootstrap(),
    api.daemons(),
    api.identitiesWithBindings(),
  ]);
  return { bootstrap, daemons, identities };
}

async function settingsBoot() {
  const { bootstrap, daemons, identities } = await bootstrapContext();
  const local = preferredDaemon(daemons);
  return {
    serverUrl: window.location.origin,
    autoStart: false,
    startCommand: '',
    stopCommand: '',
    autoStartDaemon: false,
    daemonStartCommand: '',
    daemonStopCommand: '',
    daemonRunning: daemons.some((daemon) => daemon.online),
    language: language(),
    state: 'online',
    latencyMs: await api.ping(),
    daemons,
    identities,
    localDaemonId: local?.daemon_id ?? null,
    workspaceRoot: projectPath(bootstrap.default_project),
    capabilities: {
      lifecycleControl: false,
      daemonLifecycle: false,
      addDaemon: false,
      // Pairing goes through /api/pairing/*, which the Server answers only
      // for loopback peers. A remote browser therefore sees these controls
      // and gets a 403 rendered as an error — hiding them would not be more
      // truthful, since the browser cannot know the Server's view of its
      // source address.
      pairing: true,
      console: true,
    },
  };
}

/**
 * Run-history directories a project's chat sessions live under.
 *
 * Every run records the project's local path; runs created before that was
 * decoupled recorded the project's pinned remote directory instead, so both
 * are queried until those rows age out.
 */
function projectCwds(project: ConsoleProjectInfo | null, fallback: string): string[] {
  if (!project) return [fallback];
  return [project.path, project.remote_path].filter(
    (value): value is string => Boolean(value),
  );
}

async function chatSessions(cwds: string[]): Promise<ChatSessionInfo[]> {
  const runs = await api.listRuns({ mode: 'chat', cwds });
  return runs.runs.map(toChatSession);
}

async function chatBoot() {
  const { bootstrap, daemons, identities } = await bootstrapContext();
  const project = await activeProject();
  const [runtimes] = await Promise.all([api.runtimes()]);
  const projectPathValue = project?.path ?? projectPath(bootstrap.default_project);
  return {
    language: language(),
    state: 'online',
    daemons,
    runtimes,
    identities,
    sessions: [],
    workspaceFolders: [projectPathValue],
    workspaceRoot: projectPathValue,
    project,
    localDaemonId: preferredDaemon(daemons)?.daemon_id ?? null,
    capabilities: { lifecycleControl: false, console: true },
  };
}

async function resolveChatRun(jobId: string, dir: string | null) {
  const tree = await api.jobTree(jobId);
  const node = tree.node;
  if (!node.session_id) throw new Error('chat run has no resumed session yet');
  return {
    job_id: jobId,
    task_id: node.current_task_id ?? jobId,
    session_id: node.session_id,
    provider: node.provider,
    identity_did: node.identity_did ?? null,
    daemon_id: node.daemon_id ?? null,
    dir,
  };
}

async function pollTask(taskId: string): Promise<TaskStatusInfo> {
  const deadline = Date.now() + 300_000;
  while (Date.now() < deadline) {
    const task = await api.taskStatus(taskId);
    if (!['pending', 'running'].includes(task.status)) return task;
    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }
  throw new Error('task did not finish within 300s');
}

async function runChatTurn(
  params: Params,
  project: ConsoleProjectInfo | null,
  fallbackProject: string,
  emit: Emit,
): Promise<void> {
  const daemonId = params.daemonId ? String(params.daemonId) : null;
  try {
    const currentJobId = params.currentJobId ? String(params.currentJobId) : null;
    const message = (params.message as ChatInputMessage | undefined) ?? null;
    const effectiveProject = params.agentCwd
      ? String(params.agentCwd)
      : dirForDaemon(
          project,
          daemonId,
          preferredDaemon((await bootstrapContext()).daemons)?.daemon_id ?? null,
          fallbackProject,
        );
    if (params.agentCwd) storageSet(PROJECT_KEY, effectiveProject);
    const job = currentJobId
      ? await api.appendChatInput(currentJobId, String(params.prompt ?? ''), message)
      : await api.createChatJob({
          prompt: String(params.prompt ?? ''),
          provider: String(params.provider ?? 'claude'),
          targetDaemonId: daemonId ?? '',
          identityDid: params.identityDid ? String(params.identityDid) : null,
          agentCwd: effectiveProject,
          // The run belongs to the project, not to the directory it happens
          // to execute in, so remote turns stay grouped under the project.
          cwd: project?.path ?? fallbackProject,
          message,
        });
    emit('chat', { kind: 'started', ...job });
    let final: Record<string, unknown> | null = null;
    try {
      for await (const event of api.streamTask(job.task_id)) {
        if (event.type === 'done') {
          const { stderr: _stderr, ...safe } = event;
          final = { kind: 'done', ...safe };
          emit('chat', final);
          break;
        }
        emit('chat', { kind: event.type, ...event });
      }
    } catch (err) {
      emit('chat', {
        kind: 'streamFallback',
        message: err instanceof Error ? err.message : String(err),
      });
    }
    if (!final) {
      const task = await pollTask(job.task_id);
      emit('chat', {
        kind: 'done',
        status: task.status,
        stdout: task.stdout ?? '',
        session_id: task.session_id,
        exit_code: task.exit_code,
        usage: task.usage ?? null,
        model: task.usage?.model ?? null,
      });
    }
    const [jobStatus, sessions] = await Promise.all([
      api.jobStatus(job.job_id).catch(() => null),
      chatSessions(projectCwds(project, fallbackProject)).catch(() => []),
    ]);
    emit('chat', { kind: 'settled', jobStatus, sessions });
  } catch (err) {
    emit('chat', {
      kind: 'error',
      message: err instanceof Error ? err.message : String(err),
    });
  }
}

async function goalBoot() {
  const { bootstrap, daemons, identities } = await bootstrapContext();
  const project = await activeProject();
  const projectPathValue = project?.path ?? projectPath(bootstrap.default_project);
  const list = await api.listRuns({ mode: 'goal', cwd: projectPathValue });
  const runs = list.runs.map(toGoalRun);
  let selectedRunId = storageGet(GOAL_RUN_KEY);
  if (!selectedRunId || !runs.some((run) => run.root_job_id === selectedRunId)) {
    selectedRunId = runs[0]?.root_job_id ?? null;
  }
  let tree = null;
  if (selectedRunId) {
    tree = await api.jobTree(selectedRunId).catch(() => null);
  }
  return {
    language: language(),
    state: 'online',
    runs,
    selectedRunId,
    tree,
    selectedNodeId: storageGet(GOAL_NODE_KEY),
    identities,
    daemons,
    workspaceFolder: projectPathValue,
    project,
    inspectorWidth: null,
    sidebarWidth: null,
    sidebarCollapsed: false,
    capabilities: { lifecycleControl: false, console: true },
  };
}

async function sopBoot() {
  const { bootstrap, daemons, identities } = await bootstrapContext();
  const project = await activeProject();
  const root = project?.path ?? projectPath(bootstrap.default_project);
  const templates = await api.consoleSopTemplates(root).catch(() => []);
  let path = storageGet(SOP_PATH_KEY);
  if (!path || !templates.some((template) => template.path === path)) {
    path = templates[0]?.path ?? null;
  }
  if (path) storageSet(SOP_PATH_KEY, path);
  let text = '';
  if (path) {
    text = (await api.readConsoleSopFile(path)).text;
  }
  const parsed = parseSopText(text);
  const runs = path
    ? (await api.listRuns({ mode: 'sop', cwd: root, templatePath: path })).runs
    : [];
  // Legacy global keys are considered only when the ID still belongs to the
  // template-filtered result. New writes always use a template-scoped key.
  const legacyTrackedId = storageGet(SOP_RUN_KEY);
  const legacySelectedId = storageGet(SOP_SELECTED_RUN_KEY);
  const trackedId = path
    ? storageGet(sopTrackedRunKey(path)) ?? legacyTrackedId
    : null;
  const selectedId = path
    ? storageGet(sopSelectedRunKey(path)) ??
      legacySelectedId ??
      trackedId ??
      runs[0]?.root_job_id
    : null;
  const trackedRun = runs.find((run) => run.root_job_id === trackedId);
  const selectedRun = runs.find((run) => run.root_job_id === selectedId);
  return {
    language: language(),
    state: 'online',
    templatePath: path,
    templates,
    daemons: daemons.map((daemon) => ({
      id: daemon.daemon_id,
      label: daemon.daemon_name || daemon.device_name || daemon.daemon_id,
      online: daemon.online,
      providers: [...new Set(daemon.providers.map((provider) => provider.provider))].sort(),
      url: daemon.url,
    })),
    identities: identities.filter((identity) => identity.status === 'active'),
    model: parsed.parseError ? null : parsed.model,
    text,
    dirty: false,
    positions: path ? JSON.parse(storageGet(`agt.console.sop.layout:${path}`) ?? '{}') : {},
    parseError: parsed.parseError,
    workspaceFolder: root,
    project,
    localDaemonId: preferredDaemon(daemons)?.daemon_id ?? null,
    sidebarWidth: null,
    inspectorWidth: null,
    trackedRun: trackedRun ? toRunSummary(trackedRun) : null,
    runs: runs.map((run) => toRunSummary(run, text)),
    run: selectedRun ? toRunSummary(selectedRun, text) : null,
    capabilities: { lifecycleControl: false, console: true },
  };
}

async function currentSopText(): Promise<{ path: string; text: string }> {
  const path = storageGet(SOP_PATH_KEY);
  if (!path) throw new Error('no SOP document selected');
  const file = await api.readConsoleSopFile(path);
  return { path, text: file.text };
}

async function sopApply(intent: EditIntent): Promise<void> {
  const current = await currentSopText();
  const next = serializeSop(applyIntent(parseSopText(current.text), intent));
  await api.writeConsoleSopFile(current.path, next);
}

function pickNewSopPath(templates: { path: string }[]): string {
  const root = storageGet(PROJECT_KEY);
  if (!root) throw new Error('no workspace selected');
  const existing = new Set(templates.map((item) => item.path));
  for (let index = 1; index < 10_000; index += 1) {
    const path = `${root.replace(/\/+$/, '')}/untitled-${index}.sop.yaml`;
    if (!existing.has(path)) return path;
  }
  throw new Error('unable to allocate SOP filename');
}

function projectFilePath(root: string, filename: string): string {
  const separator = root.includes('\\') ? '\\' : '/';
  return `${root.replace(/[\\/]+$/, '')}${separator}${filename}`;
}

/**
 * Fill the project's pinned remote daemon/directory into SOP nodes that pin
 * neither. A node with an explicit ``daemon`` or ``dir`` keeps the template
 * author's intent; an unpinned node would otherwise inherit the local cwd,
 * which a remote daemon cannot see.
 */
function applyProjectRemoteDefaults(
  spec: Record<string, unknown>,
  project: ConsoleProjectInfo | null,
): void {
  if (!project?.remote_daemon_id || !project.remote_path) return;
  const tasks = Array.isArray(spec.tasks) ? spec.tasks : [];
  for (const entry of tasks) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const task = entry as Record<string, unknown>;
    if (task.daemon || task.dir) continue;
    task.daemon = project.remote_daemon_id;
    task.dir = project.remote_path;
  }
}

export async function browserCall<T>(
  method: string,
  params: Params | undefined,
  emit: Emit,
): Promise<T> {
  const p = params ?? {};
  switch (method) {
    case 'projects.boot': {
      const { daemons } = await bootstrapContext();
      return {
        language: language(),
        state: 'online',
        projects: await api.consoleProjects(),
        // Only daemons the Server reverse-dials can be browsed for a remote
        // working directory; a local daemon shares this machine's paths.
        remoteDaemons: daemons
          .filter((daemon) => !isLocalDaemon(daemon))
          .map((daemon) => ({
            id: daemon.daemon_id,
            label: daemon.daemon_name || daemon.device_name || daemon.daemon_id,
            online: daemon.online,
          })),
        // Usage history refers to daemons by id; resolve it to the same
        // human-readable name the rest of the console shows.
        daemonNames: Object.fromEntries(
          daemons.map((daemon) => [
            daemon.daemon_id,
            daemon.daemon_name || daemon.device_name || daemon.daemon_id,
          ]),
        ),
        // The usage dashboard's agent filter must reflect what is actually
        // installed across every daemon, not a hard-coded provider list.
        agents: agentOptionsFromDaemons(daemons),
        capabilities: { lifecycleControl: false, console: true },
      } as T;
    }
    case 'projects.directories':
      return await api.consoleDirectories(
        p.path ? String(p.path) : null,
        p.daemonId ? String(p.daemonId) : null,
      ) as ConsoleDirectoryListing as T;
    case 'projects.pickDirectory':
      return {
        path: await api.pickConsoleDirectory(),
      } as T;
    case 'projects.create': {
      const project = await api.createConsoleProject({
        path: String(p.path ?? ''),
        name: p.name ? String(p.name) : null,
        remoteDaemonId: p.remoteDaemonId ? String(p.remoteDaemonId) : null,
        remotePath: p.remotePath ? String(p.remotePath) : null,
      });
      return { project, projects: await api.consoleProjects() } as T;
    }
    case 'projects.rename': {
      const project = await api.renameConsoleProject(
        String(p.projectId ?? ''),
        String(p.name ?? ''),
      );
      return { project, projects: await api.consoleProjects() } as T;
    }
    case 'projects.delete':
      await api.deleteConsoleProject(String(p.projectId ?? ''));
      return { projects: await api.consoleProjects() } as T;
    case 'projects.open':
      return await api.openConsoleProject(String(p.projectId ?? '')) as T;
    case 'projects.usage':
      return await api.usageSummary({
        agent: p.agent ? String(p.agent) : null,
        daemonId: p.daemonId ? String(p.daemonId) : null,
        from: String(p.from ?? ''),
        to: String(p.to ?? ''),
        bucket: (p.bucket as 'hour' | 'day' | 'month') || 'day',
      }) as T;
    case 'openProjects':
      window.location.href = '/console/projects';
      return { ok: true } as T;
    case 'boot':
      return await (pageKind() === 'chat' ? chatBoot() : settingsBoot()) as T;
    case 'testConnection': {
      const [daemons, identities] = await Promise.all([
        api.daemons(),
        api.identitiesWithBindings(),
      ]);
      return {
        state: 'online',
        latencyMs: await api.ping(),
        daemons,
        identities,
        localDaemonId: preferredDaemon(daemons)?.daemon_id ?? null,
      } as T;
    }
    case 'setConfig':
      if (p.language === 'zh' || p.language === 'en') {
        storageSet(LANGUAGE_KEY, p.language);
        document.documentElement.lang = p.language;
      }
      return { ...(await settingsBoot()), ok: true } as T;
    case 'refresh':
      return await settingsBoot() as T;
    case 'refreshDaemons': {
      // Re-read ~/.agt/daemons on the Server and reconcile its links; a
      // per-row click narrows the forced re-dial to that daemon.
      const rescan = await api.refreshDaemons(p.daemonId ? String(p.daemonId) : null);
      return { ...(await settingsBoot()), rescan } as T;
    }
    case 'startServer':
    case 'stopServer':
    case 'startDaemon':
    case 'stopDaemon':
    case 'addDaemon':
      throw new Error('Lifecycle and daemon-file management are CLI-only in the browser console');
    case 'pairingState':
      return await api.pairingState() as T;
    case 'pairingSecret':
      return await api.pairingSecret(p.reveal === true) as T;
    case 'pairingInvite':
      return await api.pairingInvite(
        String(p.name ?? ''),
        String(p.url ?? ''),
        p.daemonId ? String(p.daemonId) : null,
      ) as T;
    case 'pairingAddDaemon':
      return await api.pairingAddDaemon({
        name: String(p.name ?? '').trim(),
        url: String(p.url ?? '').trim(),
        capabilities: String(p.capabilities ?? '').trim() || null,
      }) as T;
    case 'pairingRemoveDaemon':
      return await api.pairingRemoveDaemon({
        name: p.name ? String(p.name) : undefined,
        url: p.url ? String(p.url) : undefined,
        daemonId: p.daemonId ? String(p.daemonId) : undefined,
      }) as T;
    case 'daemonConfigShow':
      return await api.daemonConfig() as T;
    case 'daemonConfigSet':
      return await api.setDaemonConfig({
        maxConcurrent: typeof p.maxConcurrent === 'number' ? p.maxConcurrent : undefined,
        agentTimeout: typeof p.agentTimeout === 'number' ? p.agentTimeout : undefined,
        workDir: typeof p.workDir === 'string' ? p.workDir : undefined,
        agentCwdAllowlist: Array.isArray(p.agentCwdAllowlist)
          ? p.agentCwdAllowlist.filter((item): item is string => typeof item === 'string')
          : undefined,
      }) as T;
    case 'identityCreate': {
      const boot = await settingsBoot();
      const created = await api.createIdentity({
        daemon_id: boot.localDaemonId ?? 'local',
        name: String(p.name ?? ''),
        slug: p.slug ? String(p.slug) : null,
        description: String(p.description ?? ''),
        capabilities: Array.isArray(p.capabilities)
          ? p.capabilities.map(String).filter(Boolean)
          : [],
      });
      return { created, identities: await api.identitiesWithBindings() } as T;
    }
    case 'identityUpdate': {
      const did = String(p.did ?? '');
      const current = (await api.identitiesWithBindings()).find((item) => item.did === did);
      if (!current) throw new Error('identity not found');
      const capabilities = Array.isArray(p.capabilities)
        ? [...new Set(p.capabilities.map(String).filter(Boolean))]
        : current.capabilities;
      const updated = await api.updateIdentity(did, {
        name: String(p.name ?? current.name),
        description: String(p.description ?? current.description),
        add_capabilities: capabilities.filter((item) => !current.capabilities.includes(item)),
        remove_capabilities: current.capabilities.filter((item) => !capabilities.includes(item)),
      });
      return { updated, identities: await api.identitiesWithBindings() } as T;
    }
    case 'identityBindingCreate':
      await api.createIdentityBinding(String(p.did ?? ''), {
        daemonId: String(p.daemonId ?? ''),
        provider: String(p.provider ?? ''),
        priority: Number(p.priority ?? 0),
        isDefault: p.isDefault === true,
      });
      return { identities: await api.identitiesWithBindings() } as T;
    case 'identityBindingUpdate':
      await api.updateIdentityBinding(String(p.did ?? ''), Number(p.bindingId), {
        priority: p.priority === undefined ? undefined : Number(p.priority),
        isDefault: typeof p.isDefault === 'boolean' ? p.isDefault : undefined,
      });
      return { identities: await api.identitiesWithBindings() } as T;
    case 'identityBindingDelete':
      await api.deleteIdentityBinding(String(p.did ?? ''), Number(p.bindingId));
      return { identities: await api.identitiesWithBindings() } as T;
    case 'openChat':
    case 'openSop':
    case 'openGoal': {
      const page = method.slice(4).toLowerCase() as 'chat' | 'sop' | 'goal';
      window.location.href = modeUrl(page);
      return { ok: true } as T;
    }
    case 'openSettings': {
      const projectId = routeProjectId();
      window.location.href = projectId
        ? `/console/settings?returnTo=${encodeURIComponent(`/console/projects/${projectId}/chat`)}`
        : '/console/settings';
      return { ok: true } as T;
    }
    case 'setLanguage':
      if (p.language === 'zh' || p.language === 'en') {
        storageSet(LANGUAGE_KEY, p.language);
        document.documentElement.lang = p.language;
      }
      return { ok: true } as T;
    case 'openExternal': {
      const url = new URL(String(p.url ?? ''));
      if (!['http:', 'https:', 'mailto:'].includes(url.protocol)) {
        throw new Error('unsupported external link');
      }
      window.open(url.href, '_blank', 'noopener,noreferrer');
      return { ok: true } as T;
    }
    case 'sessions': {
      const boot = await bootstrapContext();
      const project = await activeProject();
      // Sessions belong to the project, not to the daemon currently selected,
      // so a remote run shows up next to local ones (each row carries its
      // own daemon_id for display and resume).
      return {
        sessions: await chatSessions(
          projectCwds(project, projectPath(boot.bootstrap.default_project)),
        ),
      } as T;
    }
    case 'selectSession': {
      if (!p.jobId) throw new Error('chat session job id is required');
      const resolved = await resolveChatRun(
        String(p.jobId),
        p.dir ? String(p.dir) : null,
      );
      // A run row's ``cwd`` is the project's local path (its run-history
      // key); the working directory a resumed session actually uses lives
      // on the owning daemon and only the Server knows it.
      const session = await api
        .resolveSession(resolved.session_id)
        .catch(() => null);
      resolved.dir = session?.dir ?? resolved.dir;
      const [jobStatus, history] = await Promise.all([
        api.jobStatus(resolved.job_id),
        api.sessionHistory(resolved.session_id).catch((err: unknown) => err),
      ]);
      return {
        resolved,
        jobStatus,
        history: history instanceof Error ? undefined : history as SessionHistoryInfo,
        historyError: history instanceof Error ? history.message : undefined,
      } as T;
    }
    case 'send': {
      const boot = await bootstrapContext();
      const project = await activeProject();
      void runChatTurn(
        p,
        project,
        project?.path ?? projectPath(boot.bootstrap.default_project),
        emit,
      );
      return { accepted: true } as T;
    }
    case 'upload': {
      const contentB64 = String(p.contentB64 ?? '');
      if (!contentB64) throw new Error('upload content is required');
      return await api.uploadInput({
        name: String(p.name ?? ''),
        contentB64,
        mimeType: p.mimeType ? String(p.mimeType) : null,
        source: 'console',
      }) as T;
    }
    case 'deleteUpload': {
      const uploadId = String(p.uploadId ?? '');
      if (uploadId) await api.deleteInputUpload(uploadId).catch(() => {});
      return { ok: true } as T;
    }
    case 'stop':
      return await api.stopChatJob(String(p.jobId ?? '')) as T;
    case 'deleteSession': {
      const jobId = String(p.jobId ?? '');
      let sessionId = String(p.sessionId ?? '');
      let provider = String(p.provider ?? '');
      let daemonId = String(p.daemonId ?? '');
      if (jobId) {
        const resolved = await resolveChatRun(jobId, null);
        sessionId = resolved.session_id;
        provider = resolved.provider;
        // The owning daemon scopes the delete; the webview's selected
        // daemon is only a fallback (it may not own this session).
        daemonId = resolved.daemon_id ?? daemonId;
      }
      const deleted = await api.deleteSession(sessionId, {
        provider,
        daemonId,
        finalizeActive: true,
      });
      const boot = await bootstrapContext();
      const project = await activeProject();
      return {
        deleted,
        sessions: await chatSessions(
          projectCwds(project, projectPath(boot.bootstrap.default_project)),
        ),
      } as T;
    }
    case 'goal.boot':
      return await goalBoot() as T;
    case 'goal.select':
      storageSet(GOAL_RUN_KEY, String(p.rootJobId ?? ''));
      storageSet(GOAL_NODE_KEY, null);
      return await goalBoot() as T;
    case 'goal.tree':
      return await api.jobTree(String(p.rootJobId ?? '')) as T;
    case 'goal.nodeLog':
      return await api.jobLog(String(p.jobId ?? '')) as T;
    case 'goal.taskStatus':
      return await api.taskStatus(String(p.taskId ?? '')) as T;
    case 'goal.submit': {
      const boot = await bootstrapContext();
      const project = await activeProject();
      const projectPathValue = project?.path ?? projectPath(boot.bootstrap.default_project);
      const daemonId = p.daemonId ? String(p.daemonId) : null;
      const defaultDir = dirForDaemon(
        project,
        daemonId,
        preferredDaemon(boot.daemons)?.daemon_id ?? null,
        projectPathValue,
      );
      const submitted = await api.submitGoal({
        goal: String(p.goal ?? ''),
        identityDid: p.identityDid ? String(p.identityDid) : null,
        provider: p.provider ? String(p.provider) : null,
        daemonId,
        dir: p.dir ? String(p.dir) : defaultDir || projectPathValue,
        cwd: projectPathValue,
        maxDepth: Number(p.maxDepth ?? 5),
        maxDescendants: Number(p.maxDescendants ?? 64),
      });
      storageSet(GOAL_RUN_KEY, submitted.root_job_id);
      storageSet(GOAL_NODE_KEY, submitted.root_job_id);
      return { ...submitted, ...(await goalBoot()) } as T;
    }
    case 'goal.answer':
      await api.appendChatInput(String(p.jobId ?? ''), String(p.answer ?? ''));
      return await goalBoot() as T;
    case 'goal.cancel':
      return await api.cancelJob(String(p.rootJobId ?? '')) as T;
    case 'goal.export': {
      const rootJobId = String(p.rootJobId ?? '');
      const exported = await api.goalExport(
        rootJobId,
        p.includeFailed === true,
        p.keepPlanning === true,
      );
      const project = await activeProject();
      if (!project) throw new Error('no project selected');
      const safeRootId = rootJobId.replace(/[^a-zA-Z0-9._-]/g, '-');
      const filename = `${safeRootId || 'goal'}.sop.yaml`;
      const path = projectFilePath(project.path, filename);
      await api.writeConsoleSopFile(path, exported.yaml);
      return {
        canceled: false,
        path,
        warnings: exported.warnings,
      } as T;
    }
    case 'goal.setInspectorWidth':
    case 'goal.setSidebarWidth':
      return { ok: true, width: p.width } as T;
    case 'goal.setSidebarCollapsed':
      return { ok: true, collapsed: p.collapsed } as T;
    case 'sop.boot':
      return await sopBoot() as T;
    case 'sop.openTemplate':
      storageSet(SOP_PATH_KEY, String(p.path ?? ''));
      return await sopBoot() as T;
    case 'sop.newSop': {
      const current = await sopBoot();
      const path = pickNewSopPath(current.templates);
      await api.writeConsoleSopFile(path, 'name: untitled\ntasks:\n  - name: task\n    input: ""\n');
      storageSet(SOP_PATH_KEY, path);
      return await sopBoot() as T;
    }
    case 'sop.apply':
      await sopApply(p.intent as EditIntent);
      return await sopBoot() as T;
    case 'sop.renameFile': {
      const sourcePath = String(p.path ?? '');
      const renamed = await api.renameConsoleSopFile(
        sourcePath,
        String(p.newName ?? ''),
      );
      if (storageGet(SOP_PATH_KEY) === sourcePath) {
        storageSet(SOP_PATH_KEY, renamed.path);
      }
      for (const [oldKey, newKey] of [
        [sopTrackedRunKey(sourcePath), sopTrackedRunKey(renamed.path)],
        [sopSelectedRunKey(sourcePath), sopSelectedRunKey(renamed.path)],
        [
          `agt.console.sop.layout:${sourcePath}`,
          `agt.console.sop.layout:${renamed.path}`,
        ],
      ] as const) {
        const value = storageGet(oldKey);
        if (value !== null) {
          storageSet(newKey, value);
          storageSet(oldKey, null);
        }
      }
      return await sopBoot() as T;
    }
    case 'sop.save':
      return await sopBoot() as T;
    case 'sop.validate': {
      const current = await currentSopText();
      const validation = await api.validateSop(current.text);
      return { ...validation, at: Date.now() } as T;
    }
    case 'sop.run': {
      const current = await currentSopText();
      const parsed = parseSopText(current.text);
      if (parsed.parseError) throw new Error(parsed.parseError);
      const boot = await bootstrapContext();
      const project = await activeProject();
      const spec = parsed.doc.toJS() as Record<string, unknown>;
      applyProjectRemoteDefaults(spec, project);
      const submitted = await api.submitSop({
        spec,
        cwd: project?.path ?? projectPath(boot.bootstrap.default_project),
        localDaemonId: preferredDaemon(boot.daemons)?.daemon_id ?? null,
        templatePath: current.path,
        templateHash: fingerprint(current.text),
      });
      storageSet(sopTrackedRunKey(current.path), submitted.root_job_id);
      storageSet(sopSelectedRunKey(current.path), submitted.root_job_id);
      return {
        rootJobId: submitted.root_job_id,
        nodes: submitted.nodes,
        ...(await sopBoot()),
      } as T;
    }
    case 'sop.status':
      return await api.sopStatus(String(p.rootJobId ?? '')) as T;
    case 'sop.nodeLog':
      return await api.sopNodeLog(
        String(p.rootJobId ?? ''),
        String(p.node ?? ''),
      ) as T;
    case 'sop.cancelRun': {
      const status = await api.sopStatus(String(p.rootJobId ?? ''));
      const pending = status.nodes.filter(
        (node) => !['done', 'failed', 'cancelled'].includes(node.status),
      );
      await Promise.all(pending.map((node) => api.cancelJob(node.job_id)));
      return { cancelled: pending.map((node) => node.name) } as T;
    }
    case 'sop.selectRun':
      storageSet(
        sopSelectedRunKey(storageGet(SOP_PATH_KEY) ?? ''),
        String(p.rootJobId ?? ''),
      );
      return await sopBoot() as T;
    case 'sop.forgetRun':
      storageSet(sopTrackedRunKey(storageGet(SOP_PATH_KEY) ?? ''), null);
      return await sopBoot() as T;
    case 'sop.revealYaml': {
      const current = await currentSopText();
      const url = URL.createObjectURL(new Blob([current.text], { type: 'text/yaml' }));
      window.open(url, '_blank', 'noopener,noreferrer');
      window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
      return { ok: true } as T;
    }
    case 'sop.setLayout': {
      const path = storageGet(SOP_PATH_KEY);
      if (path) {
        storageSet(
          `agt.console.sop.layout:${path}`,
          JSON.stringify((p.positions as Record<string, LayoutPoint>) ?? {}),
        );
      }
      return { ok: true } as T;
    }
    case 'sop.setInspectorWidth':
    case 'sop.setSidebarWidth':
      return { ok: true, width: p.width } as T;
    case 'sop.restartServer':
      throw new Error('Server restart is CLI-only in the browser console');
    case 'openArtifact': {
      const uri = String(p.artifactUri ?? '');
      const artifactId = uri.split('/').pop();
      if (!artifactId) throw new Error('artifact id is required');
      const meta = await api.artifact(artifactId);
      const bytes = await api.artifactContent(artifactId);
      download(meta.name, bytes.slice().buffer, meta.mime_type);
      return { ok: true } as T;
    }
    default:
      throw new Error(`browser console does not implement method: ${method}`);
  }
}

