import './styles.css';
import './mode-switcher.css';
import './markdown.css';
import './goal.css';
import { providerLogo } from '../core/icons';
import { renderMarkdown } from './markdown';
import { t, type MessageKey } from '../core/i18n';
import type {
  GoalJobTreeNode,
  GoalRunInfo,
  GoalTreeInfo,
  IdentityWithBindings,
  DaemonInfo,
  JobLogInfo,
  TaskStatusInfo,
  ServerState,
  UiLanguage,
} from '../core/types';
import { bridge } from './bridge';

interface GoalBoot {
  language: UiLanguage;
  state: ServerState;
  runs: GoalRunInfo[];
  selectedRunId: string | null;
  tree: GoalTreeInfo | null;
  selectedNodeId: string | null;
  identities: IdentityWithBindings[];
  daemons: DaemonInfo[];
  workspaceFolder: string | null;
  inspectorWidth: number | null;
  sidebarWidth: number | null;
  sidebarCollapsed: boolean;
}

type BusyKind = 'submit' | 'answer' | 'cancel' | 'export' | null;

let boot: GoalBoot | null = null;
let lang: UiLanguage = 'zh';
let selectedNodeId: string | null = null;
let nodeLog: JobLogInfo | null = null;
let nodeLogJobId: string | null = null;
let liveTask: TaskStatusInfo | null = null;
let liveTaskFor: string | null = null;
let busy: BusyKind = null;
let treeLoading = false;
let logLoading = false;
const logRequests = new Map<string, Promise<void>>();
let notice: string | null = null;
let pollTimer: number | null = null;
let draft = {
  goal: '',
  identityDid: '',
  provider: '',
  daemonId: '',
  dir: '',
  maxDepth: 5,
  maxDescendants: 64,
};

const INSPECTOR_THRESHOLD = 1080;
const DEFAULT_SIDEBAR_WIDTH = 302;
const MIN_SIDEBAR_WIDTH = 220;
const MAX_SIDEBAR_WIDTH = 520;
let viewportWidth = window.innerWidth;
let showInspector = viewportWidth >= INSPECTOR_THRESHOLD;
let modalNodeId: string | null = null;
let runMetaModalOpen = false;
let sidebarWidth = DEFAULT_SIDEBAR_WIDTH;
let sidebarCollapsed = false;
let viewportListenerWired = false;

function tr(key: MessageKey, vars?: Record<string, string | number>): string {
  return t(lang, key, vars);
}

function esc(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function statusPill(status: string): string {
  return `<span class="agd-goal-pill is-${esc(status)}"><i></i>${esc(statusText(status))}</span>`;
}

function clampInspectorWidth(width: number, containerWidth: number): number {
  const max = Math.max(320, containerWidth - 302 - 420);
  return Math.max(300, Math.min(max, width));
}

function applyInspectorWidth(width: number | null): void {
  const root = document.querySelector<HTMLElement>('.agd-sop');
  if (root && width !== null) {
    root.style.setProperty('--agd-goal-inspector-width', `${Math.round(width)}px`);
  }
}

function clampSidebarWidth(width: number, containerWidth: number): number {
  const reserved = showInspector ? 300 + 6 + 360 : 360;
  const max = Math.max(
    MIN_SIDEBAR_WIDTH,
    Math.min(MAX_SIDEBAR_WIDTH, containerWidth - reserved),
  );
  return Math.max(MIN_SIDEBAR_WIDTH, Math.min(max, width));
}

function applySidebarWidth(width: number): void {
  const root = document.querySelector<HTMLElement>('.agd-sop');
  root?.style.setProperty(
    '--agd-goal-sidebar-width',
    `${Math.round(width)}px`,
  );
}

function wireSidebarResizer(): void {
  const handle = document.getElementById('agd-goal-sidebar-resizer');
  const container = document.querySelector<HTMLElement>('.agd-goal-body');
  if (!handle || !container || sidebarCollapsed) return;

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    container.classList.add('is-resizing');

    const move = (moveEvent: PointerEvent): void => {
      const rect = container.getBoundingClientRect();
      sidebarWidth = clampSidebarWidth(
        moveEvent.clientX - rect.left,
        rect.width,
      );
      if (boot) boot.sidebarWidth = sidebarWidth;
      applySidebarWidth(sidebarWidth);
    };

    const finish = (): void => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      container.classList.remove('is-resizing');
      void bridge.call('goal.setSidebarWidth', { width: sidebarWidth });
    };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  });
}

async function toggleSidebar(): Promise<void> {
  sidebarCollapsed = !sidebarCollapsed;
  if (boot) boot.sidebarCollapsed = sidebarCollapsed;
  render();
  try {
    await bridge.call('goal.setSidebarCollapsed', {
      collapsed: sidebarCollapsed,
    });
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    render();
  }
}

function wireInspectorResizer(): void {
  const handle = document.getElementById('agd-goal-inspector-resizer');
  const container = document.querySelector<HTMLElement>('.agd-goal-body');
  if (!handle || !container) return;

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    container.classList.add('is-resizing');

    const move = (moveEvent: PointerEvent): void => {
      const rect = container.getBoundingClientRect();
      const width = clampInspectorWidth(rect.right - moveEvent.clientX, rect.width);
      if (boot) boot.inspectorWidth = width;
      applyInspectorWidth(width);
    };

    const finish = (): void => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      container.classList.remove('is-resizing');
      if (boot?.inspectorWidth !== null && boot?.inspectorWidth !== undefined) {
        void bridge.call('goal.setInspectorWidth', { width: boot.inspectorWidth });
      }
    };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  });
}

function statusText(status: string): string {
  const key = `sop.status.${status}` as MessageKey;
  const translated = t(lang, key);
  return translated === key ? status : translated;
}

function updateViewport(): void {
  const width = window.innerWidth;
  if (width === viewportWidth) return;
  viewportWidth = width;
  const next = width >= INSPECTOR_THRESHOLD;
  if (next !== showInspector) {
    showInspector = next;
    modalNodeId = null;
    render();
  }
}

async function openNodeModal(jobId: string): Promise<void> {
  if (!boot?.tree) return;
  modalNodeId = jobId;
  if (selectedNodeId !== jobId) {
    selectedNodeId = jobId;
    nodeLog = null;
    nodeLogJobId = null;
  }
  await loadNodeLog(jobId, true);
  render();
}

function closeNodeModal(): void {
  modalNodeId = null;
  render();
}

function openRunMetaModal(): void {
  if (!selectedRun()) return;
  runMetaModalOpen = true;
  render();
}

function closeRunMetaModal(): void {
  runMetaModalOpen = false;
  render();
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const date = new Date(value.replace(' ', 'T') + 'Z');
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.round(seconds));
  const minutes = Math.floor(total / 60);
  const rest = total % 60;
  return minutes > 0 ? `${minutes}m${String(rest).padStart(2, '0')}s` : `${rest}s`;
}

function selectedNode(): GoalJobTreeNode | null {
  const find = (node: GoalJobTreeNode): GoalJobTreeNode | null => {
    if (node.job_id === selectedNodeId) return node;
    for (const child of node.children) {
      const found = find(child);
      if (found) return found;
    }
    return null;
  };
  return boot?.tree?.node ? find(boot.tree.node) : null;
}

function selectedRun(): GoalRunInfo | null {
  if (!boot?.selectedRunId) return null;
  const run = boot.runs.find((item) => item.root_job_id === boot?.selectedRunId);
  if (run) return run;
  if (!boot.tree || boot.tree.root_job_id !== boot.selectedRunId) return null;
  const root = boot.tree.node;
  return {
    root_job_id: boot.tree.root_job_id,
    goal: boot.tree.goal?.goal ?? root.label,
    status: root.status,
    provider: root.provider,
    identity_did: root.identity_did,
    daemon_id: root.daemon_id,
    cwd: null,
    created_at: '',
    updated_at: '',
    total_nodes: flatten(root).length,
    status_counts: {},
    meta: boot.tree.goal ?? {
      goal: root.label,
      template: null,
      budget: null,
      goal_sha256: null,
    },
  };
}

function identityLabel(did: string | null | undefined): string {
  if (!did) return tr('sop.field.none');
  const identity = boot?.identities.find((item) => item.did === did);
  return identity ? `${identity.name} · ${did}` : did;
}

function daemonLabel(daemonId: string | null | undefined): string {
  if (!daemonId) return tr('sop.field.none');
  const daemon = boot?.daemons.find((item) => item.daemon_id === daemonId);
  if (!daemon) return daemonId;
  return `${daemon.daemon_name || daemon.device_name || daemon.daemon_id} · ${daemonId}`;
}

function runRunning(): boolean {
  const status = boot?.tree?.node.status;
  return status !== 'done' && status !== 'failed' && status !== 'cancelled';
}

function flatten(node: GoalJobTreeNode, out: GoalJobTreeNode[] = []): GoalJobTreeNode[] {
  out.push(node);
  node.children.forEach((child) => flatten(child, out));
  return out;
}

function applyBoot(next: GoalBoot): void {
  boot = next;
  lang = next.language;
  sidebarWidth = next.sidebarWidth ?? sidebarWidth ?? DEFAULT_SIDEBAR_WIDTH;
  sidebarCollapsed = next.sidebarCollapsed;
  selectedNodeId = next.selectedNodeId ?? selectedNodeId;
  if (selectedNodeId && next.tree && !flatten(next.tree.node).some((node) => node.job_id === selectedNodeId)) {
    selectedNodeId = null;
  }
  if (selectedNodeId === null && next.tree?.node) {
    selectedNodeId = next.tree.node.job_id;
  }
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function startPolling(): void {
  stopPolling();
  if (!boot?.tree || !runRunning()) return;
  pollTimer = window.setTimeout(async () => {
    pollTimer = null;
    const rootId = boot?.selectedRunId;
    if (!rootId) return;
    try {
      const tree = await bridge.call<GoalTreeInfo>('goal.tree', { rootJobId: rootId });
      if (boot?.selectedRunId !== rootId) return;
      applyBoot({ ...boot, tree, selectedNodeId });
      renderMainOnly();
      if (selectedNodeId) void loadNodeLog(selectedNodeId, false);
      if (runRunning()) startPolling();
    } catch (err) {
      notice = err instanceof Error ? err.message : String(err);
      renderMainOnly();
    }
  }, 1600);
}

async function loadNodeLog(jobId: string, showLoading = true): Promise<void> {
  const pending = logRequests.get(jobId);
  if (pending) {
    return pending;
  }
  const request = (async (): Promise<void> => {
    logLoading = true;
    if (showLoading) renderMainOnly();
    try {
      const result = await bridge.call<JobLogInfo>('goal.nodeLog', { jobId });
      if (selectedNodeId === jobId) {
        nodeLog = result;
        nodeLogJobId = jobId;
      }
      const node = selectedNode();
      if (node?.job_id === jobId && node.current_task_id && (node.status === 'running' || node.status === 'pending')) {
        liveTask = await bridge.call<TaskStatusInfo>('goal.taskStatus', { taskId: node.current_task_id });
        liveTaskFor = jobId;
      }
    } catch {
      if (selectedNodeId === jobId) {
        nodeLog = null;
        nodeLogJobId = jobId;
        liveTask = null;
        liveTaskFor = null;
      }
    } finally {
      logLoading = false;
      logRequests.delete(jobId);
      renderMainOnly();
    }
  })();
  logRequests.set(jobId, request);
  return request;
}

async function selectRun(rootJobId: string): Promise<void> {
  stopPolling();
  treeLoading = true;
  selectedNodeId = null;
  nodeLog = null;
  nodeLogJobId = null;
  boot = boot ? { ...boot, selectedRunId: rootJobId, tree: null } : boot;
  render();
  try {
    const payload = await bridge.call<GoalBoot>('goal.select', { rootJobId });
    applyBoot(payload);
    if (selectedNodeId) void loadNodeLog(selectedNodeId, false);
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
  } finally {
    treeLoading = false;
    render();
    startPolling();
  }
}

async function refreshRuns(): Promise<void> {
  const payload = await bridge.call<GoalBoot>('goal.boot');
  applyBoot(payload);
  render();
  startPolling();
}

async function submitGoal(): Promise<void> {
  if (busy || draft.goal.trim().length === 0) return;
  busy = 'submit';
  notice = null;
  render();
  try {
    const payload = await bridge.call<GoalBoot>('goal.submit', draft);
    applyBoot(payload);
    draft = { ...draft, goal: '' };
    if (selectedNodeId) void loadNodeLog(selectedNodeId, false);
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
  } finally {
    busy = null;
    render();
    startPolling();
  }
}

async function answerQuestion(jobId: string): Promise<void> {
  const input = document.getElementById('agd-goal-answer') as HTMLTextAreaElement | null;
  const answer = input?.value.trim() ?? '';
  if (busy || answer.length === 0) return;
  busy = 'answer';
  render();
  try {
    const payload = await bridge.call<GoalBoot>('goal.answer', { jobId, answer });
    applyBoot(payload);
    if (selectedNodeId) void loadNodeLog(selectedNodeId, false);
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
  } finally {
    busy = null;
    render();
    startPolling();
  }
}

async function cancelRun(): Promise<void> {
  const rootId = boot?.selectedRunId;
  if (!rootId || busy) return;
  busy = 'cancel';
  render();
  try {
    await bridge.call('goal.cancel', { rootJobId: rootId });
    await refreshRuns();
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    render();
  } finally {
    busy = null;
    render();
  }
}

async function exportSop(): Promise<void> {
  const rootId = boot?.selectedRunId;
  if (!rootId || busy) return;
  busy = 'export';
  render();
  try {
    await bridge.call('goal.export', { rootJobId: rootId });
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    render();
  } finally {
    busy = null;
    render();
  }
}

function renderHeader(): string {
  return `
  <header class="agd-sop-head">
    <div class="agd-sop-brand"><span>AGT</span><small>${tr('goal.title')}</small></div>
    <button type="button" class="agd-icon-btn agd-goal-sidebar-toggle" id="agd-goal-sidebar-toggle"
      aria-expanded="${sidebarCollapsed ? 'false' : 'true'}"
      title="${sidebarCollapsed ? tr('goal.sidebarExpand') : tr('goal.sidebarCollapse')}">
      ${sidebarCollapsed ? '»' : '«'}
    </button>
    <nav class="agd-modes" aria-label="AGT modes">
      <button type="button" class="agd-mode" data-mode="chat">${tr('mode.chat')}</button>
      <button type="button" class="agd-mode" data-mode="sop">${tr('mode.sop')}</button>
      <button type="button" class="agd-mode agd-mode-active" data-mode="goal" aria-current="page">${tr('mode.goal')}</button>
    </nav>
    <div class="agd-sop-tools">
      <span class="agd-goal-state is-${esc(boot?.state ?? 'unknown')}"><i></i>${tr(`status.${boot?.state ?? 'unknown'}` as MessageKey)}</span>
      <button type="button" class="agd-icon-btn" id="agd-goal-refresh" title="${tr('goal.refresh')}">↻</button>
      <button type="button" class="agd-icon-btn" id="agd-goal-lang">${tr('settings.langToggle')}</button>
      <button type="button" class="agd-icon-btn" id="agd-goal-settings" title="${tr('chat.openSettings')}">⚙</button>
    </div>
  </header>`;
}

function renderSubmitForm(): string {
  const identities = (boot?.identities ?? [])
    .filter((identity) => identity.status === 'active');
  const identityOptions = [`<option value="">${tr('chat.identityNone')}</option>`]
    .concat(identities.map((identity) => `<option value="${esc(identity.did)}" ${draft.identityDid === identity.did ? 'selected' : ''}>${esc(identity.name)} · ${esc(identity.did)}</option>`))
    .join('');
  const daemons = boot?.daemons ?? [];
  const providers = [...new Set(daemons.flatMap((daemon) => daemon.providers.map((provider) => provider.provider)))].sort();
  const providerOptions = [`<option value="">${tr('sop.field.none')}</option>`]
    .concat(providers.map((provider) => `<option value="${esc(provider)}" ${draft.provider === provider ? 'selected' : ''}>${esc(provider)}</option>`))
    .join('');
  const daemonOptions = [`<option value="">${tr('sop.field.none')}</option>`]
    .concat(daemons.map((daemon) => `<option value="${esc(daemon.daemon_id)}" ${draft.daemonId === daemon.daemon_id ? 'selected' : ''}>${esc(daemon.daemon_name || daemon.device_name || daemon.daemon_id)}</option>`))
    .join('');
  return `
  <section class="agd-sop-card agd-goal-form">
    <div class="agd-sop-card-head"><span>${tr('goal.newGoal')}</span></div>
    <textarea id="agd-goal-input" class="agd-input agd-goal-textarea" placeholder="${tr('goal.placeholder')}">${esc(draft.goal)}</textarea>
    <div class="agd-goal-form-hint">
      <span class="agd-chip"><i class="agd-goal-chip-dot"></i>${tr('goal.maxDepth')} ${draft.maxDepth}</span>
      <span class="agd-chip"><i class="agd-goal-chip-dot"></i>${tr('goal.maxDescendants')} ${draft.maxDescendants}</span>
    </div>
    <label class="agd-sop-field"><span>${tr('goal.identity')}</span><select id="agd-goal-identity" class="agd-input">${identityOptions}</select></label>
    <div class="agd-goal-grid">
      <label class="agd-sop-field"><span>${tr('goal.provider')}</span><select id="agd-goal-provider" class="agd-input">${providerOptions}</select></label>
      <label class="agd-sop-field"><span>${tr('goal.daemon')}</span><select id="agd-goal-daemon" class="agd-input">${daemonOptions}</select></label>
    </div>
    <label class="agd-sop-field"><span>${tr('goal.dir')}</span><input id="agd-goal-dir" class="agd-input agd-mono" value="${esc(draft.dir)}" placeholder="/absolute/path"></label>
    <div class="agd-goal-grid">
      <label class="agd-sop-field"><span>${tr('goal.maxDepth')}</span><input id="agd-goal-depth" class="agd-input" type="number" min="1" max="10" value="${draft.maxDepth}"></label>
      <label class="agd-sop-field"><span>${tr('goal.maxDescendants')}</span><input id="agd-goal-descendants" class="agd-input" type="number" min="1" max="512" value="${draft.maxDescendants}"></label>
    </div>
    <button type="button" class="agd-btn agd-btn-primary agd-goal-submit" id="agd-goal-submit" ${busy || draft.goal.trim() === '' ? 'disabled' : ''}>
      ${busy === 'submit' ? tr('goal.submitting') : tr('goal.submit')}
    </button>
  </section>`;
}

function renderRuns(): string {
  const runs = boot?.runs ?? [];
  if (runs.length === 0) {
    return `<section class="agd-sop-card"><div class="agd-sop-card-head"><span>${tr('goal.history')}</span></div><p class="agd-sop-empty agd-dim">${tr('goal.noRuns')}</p></section>`;
  }
  return `
  <section class="agd-sop-card agd-goal-runs-card">
    <div class="agd-sop-card-head"><span>${tr('goal.history')}</span><span class="agd-flex"></span><small class="agd-dim">${runs.length}</small></div>
    <div class="agd-goal-runs">
      ${runs.map((run) => `
        <button type="button" class="agd-goal-run ${run.root_job_id === boot?.selectedRunId ? 'is-active' : ''}" data-run="${esc(run.root_job_id)}">
          <span class="agd-goal-run-line">
            ${providerLogo(run.provider)}
            <strong>${esc(run.goal)}</strong>
          </span>
          <span class="agd-goal-run-line">
            ${statusPill(run.status)}
            <small class="agd-mono">${esc(run.root_job_id.slice(0, 8))}</small>
            <small>${esc(formatDate(run.updated_at))}</small>
          </span>
        </button>
      `).join('')}
    </div>
  </section>`;
}

function renderSidebar(): string {
  return `<aside class="agd-sop-side">${renderSubmitForm()}${renderRuns()}</aside>`;
}

function renderTreeNode(node: GoalJobTreeNode, depth: number): string {
  const childRows = node.children
    .map((child) => renderTreeNode(child, depth + 1))
    .join('');
  const ask = node.ask_question ? `<span class="agd-goal-ask" title="${esc(node.ask_question)}">ASK</span>` : '';
  const artifacts = node.artifacts.length > 0 ? `<span class="agd-goal-badge" title="${tr('goal.artifacts')}">${node.artifacts.length}</span>` : '';
  return `
    <button type="button" class="agd-goal-node is-${esc(node.status)} ${node.job_id === selectedNodeId ? 'is-selected' : ''}" data-node="${esc(node.job_id)}" style="--depth:${depth}">
      <span class="agd-goal-node-rail"><i></i></span>
      ${providerLogo(node.provider, 'md')}
      <span class="agd-goal-node-main">
        <span class="agd-goal-node-title">${esc(node.label || node.job_id.slice(0, 8))}</span>
        <small>${esc(node.provider)}${node.daemon_id ? ` · ${esc(node.daemon_id.slice(0, 8))}` : ''}${node.duration === null ? '' : ` · ${formatDuration(node.duration)}`}</small>
      </span>
      <span class="agd-goal-node-side">
        ${statusPill(node.status)}
        ${ask}
        ${artifacts}
      </span>
    </button>
  ${childRows}`;
}

function renderTree(): string {
  if (treeLoading || (!boot?.tree && boot?.selectedRunId)) {
    return `<section class="agd-sop-main"><p class="agd-sop-empty agd-dim">${tr('goal.loading')}</p></section>`;
  }
  if (!boot?.tree) {
    return `<section class="agd-sop-main"><p class="agd-sop-empty agd-dim">${tr('goal.selectRun')}</p></section>`;
  }
  return `
  <section class="agd-sop-main">
    <div class="agd-goal-hero is-actionable" id="agd-goal-overview" role="button" tabindex="0"
      title="${tr('goal.overviewHint')}" aria-label="${tr('goal.overviewHint')}">
      <div class="agd-goal-hero-main">
        <div class="agd-goal-hero-top">
          ${statusPill(boot.tree.node.status)}
          <span class="agd-mono">${esc(boot.tree.root_job_id.slice(0, 8))}</span>
          <small class="agd-goal-overview-hint">${tr('goal.overviewHint')}</small>
        </div>
        <strong title="${esc(boot.tree.goal?.goal || '')}">${esc(boot.tree.goal?.goal || boot.tree.node.label || boot.tree.root_job_id)}</strong>
        <div class="agd-goal-stats">
          <span class="agd-chip"><i class="agd-goal-chip-dot is-total"></i>${tr('goal.nodes')} ${flatten(boot.tree.node).length}</span>
          <span class="agd-chip"><i class="agd-goal-chip-dot is-running"></i>running ${flatten(boot.tree.node).filter((node) => node.status === 'running').length}</span>
          <span class="agd-chip"><i class="agd-goal-chip-dot is-done"></i>done ${flatten(boot.tree.node).filter((node) => node.status === 'done').length}</span>
          <span class="agd-chip"><i class="agd-goal-chip-dot is-failed"></i>failed ${flatten(boot.tree.node).filter((node) => node.status === 'failed').length}</span>
        </div>
      </div>
      <div class="agd-goal-hero-actions">
        <button type="button" class="agd-btn" id="agd-goal-export" ${busy ? 'disabled' : ''}>${busy === 'export' ? tr('goal.exporting') : tr('goal.export')}</button>
        <button type="button" class="agd-btn agd-btn-danger" id="agd-goal-cancel" ${busy || !runRunning() ? 'disabled' : ''}>${busy === 'cancel' ? tr('goal.cancelling') : tr('goal.cancel')}</button>
      </div>
    </div>
    <div class="agd-goal-tree">${renderTreeNode(boot.tree.node, 0)}</div>
  </section>`;
}

function renderLog(node: GoalJobTreeNode): string {
  const jobId = node.job_id;
  if (nodeLogJobId !== jobId) {
    return `<p class="agd-sop-empty agd-dim">${logLoading ? tr('goal.loading') : tr('sop.nodeLogEmpty')}</p>`;
  }
  const live = liveTaskFor === jobId && liveTask && (liveTask.status === 'pending' || liveTask.status === 'running')
    ? [{
        task_id: liveTask.task_id,
        status: liveTask.status,
        session_id: liveTask.session_id,
        exit_code: liveTask.exit_code,
        stdout: liveTask.stdout,
        stderr: liveTask.stderr,
      }]
    : [];
  // The Job log already contains the current Step. Filter it out when the
  // live status preview is present, otherwise the same task renders twice.
  const steps = [
    ...live,
    ...(nodeLog?.steps ?? []).filter(
      (step) => !live.some((item) => item.task_id === step.task_id),
    ),
  ];
  if (steps.length === 0) return `<p class="agd-sop-empty agd-dim">${tr('sop.nodeLogEmpty')}</p>`;
  return steps.map((step) => `
    <div class="agd-sop-step">
      <div class="agd-sop-step-head">
        <span class="agd-mono">${esc(step.task_id.slice(0, 8))}</span>
        <span class="agd-dim">${esc(step.status)}${step.exit_code === null ? '' : ` · exit ${step.exit_code}`}</span>
      </div>
      ${step.stdout ? `<div class="agd-sop-stdout agd-md">${renderMarkdown(step.stdout)}</div>` : ''}
      ${step.stderr ? `<div class="agd-sop-stderr agd-md">${renderMarkdown(step.stderr)}</div>` : ''}
    </div>
  `).join('');
}

function renderNodeInspector(node: GoalJobTreeNode): string {
  const artifacts = node.artifacts.length === 0
    ? `<p class="agd-sop-empty agd-dim">${tr('goal.noArtifacts')}</p>`
    : node.artifacts.map((artifact) => `
      <button type="button" class="agd-sop-artifact" data-artifact="${esc(artifact.uri)}" title="${tr('sop.artifactOpen')}">
        <strong>${esc(artifact.name)}</strong>
        <small class="agd-mono">${esc(artifact.uri)}</small>
      </button>
    `).join('');
  const ask = node.status === 'waiting_input' && node.ask_question ? `
    <div class="agd-goal-ask-card">
      <div class="agd-sop-card-head"><span>AGT-ASK</span></div>
      <p>${esc(node.ask_question)}</p>
      <textarea id="agd-goal-answer" class="agd-input agd-goal-textarea" placeholder="${tr('goal.answerPlaceholder')}"></textarea>
      <button type="button" class="agd-btn agd-btn-primary" id="agd-goal-answer-send" ${busy ? 'disabled' : ''}>${busy === 'answer' ? tr('goal.answering') : tr('goal.answer')}</button>
    </div>` : '';
  return `
    <div class="agd-sop-card">
      <div class="agd-sop-card-head"><span>${tr('goal.details')}</span><em class="agd-sop-run-status is-${esc(node.status)}">${esc(statusText(node.status))}</em></div>
      <dl class="agd-goal-meta">
        <div><dt>ID</dt><dd class="agd-mono">${esc(node.job_id)}</dd></div>
        <div><dt>${tr('goal.duration')}</dt><dd>${esc(formatDuration(node.duration))}</dd></div>
        <div><dt>${tr('chat.agent')}</dt><dd class="agd-goal-provider-line">${providerLogo(node.provider)} ${esc(node.provider)}</dd></div>
        <div><dt>${tr('goal.currentTask')}</dt><dd class="agd-mono">${esc(node.current_task_id ?? '—')}</dd></div>
        <div><dt>${tr('goal.session')}</dt><dd class="agd-mono">${esc(node.session_id ?? '—')}</dd></div>
      </dl>
      ${node.error ? `<pre class="agd-sop-stderr">${esc(node.error)}</pre>` : ''}
      ${ask}
    </div>
    <div class="agd-sop-card">
      <div class="agd-sop-card-head"><span>${tr('goal.log')}</span><span class="agd-flex"></span><button type="button" class="agd-mini-btn" id="agd-goal-log-refresh">↻</button></div>
      <div class="agd-sop-steps agd-sop-steps-lg">${renderLog(node)}</div>
    </div>
    <div class="agd-sop-card">
      <div class="agd-sop-card-head"><span>${tr('goal.artifacts')}</span></div>
      <div class="agd-sop-artifacts">${artifacts}</div>
    </div>`;
}

function renderDetails(): string {
  const node = selectedNode();
  if (!boot?.tree) return `<aside class="agd-sop-inspector"></aside>`;
  if (!node) {
    return `<aside class="agd-sop-inspector"><div class="agd-sop-card"><p class="agd-sop-empty">${tr('goal.noNode')}</p></div></aside>`;
  }
  return `<aside class="agd-sop-inspector">${renderNodeInspector(node)}</aside>`;
}

function renderRunMetaModal(): string {
  if (!runMetaModalOpen) return '';
  const run = selectedRun();
  if (!run) return '';
  const budget = run.meta.budget;
  const nodeCount =
    boot?.tree && boot.tree.root_job_id === run.root_job_id
      ? flatten(boot.tree.node).length
      : run.total_nodes;
  return `
  <div class="agd-goal-modal-backdrop" id="agd-goal-run-meta-backdrop" role="presentation">
    <div class="agd-goal-modal agd-goal-run-meta-modal" role="dialog" aria-modal="true" aria-labelledby="agd-goal-run-meta-title">
      <div class="agd-goal-modal-head">
        <span id="agd-goal-run-meta-title">${tr('goal.runMeta')}</span>
        <button type="button" class="agd-mini-btn" id="agd-goal-run-meta-close" title="${tr('goal.close')}">✕</button>
      </div>
      <div class="agd-goal-modal-body">
        <section class="agd-sop-card">
          <div class="agd-sop-card-head"><span>${tr('goal.prompt')}</span><em class="agd-sop-run-status is-${esc(run.status)}">${esc(statusText(run.status))}</em></div>
          <pre class="agd-goal-prompt">${esc(run.meta.goal || run.goal)}</pre>
        </section>
        <section class="agd-sop-card">
          <div class="agd-sop-card-head"><span>${tr('goal.overview')}</span></div>
          <dl class="agd-goal-meta agd-goal-run-meta">
            <div><dt>${tr('goal.rootJob')}</dt><dd class="agd-mono">${esc(run.root_job_id)}</dd></div>
            <div><dt>${tr('goal.provider')}</dt><dd class="agd-goal-provider-line">${providerLogo(run.provider)} ${esc(run.provider)}</dd></div>
            <div><dt>${tr('goal.daemon')}</dt><dd>${esc(daemonLabel(run.daemon_id))}</dd></div>
            <div><dt>${tr('goal.identity')}</dt><dd>${esc(identityLabel(run.identity_did))}</dd></div>
            <div><dt>${tr('goal.dir')}</dt><dd class="agd-mono">${esc(run.cwd || tr('sop.field.none'))}</dd></div>
            <div><dt>${tr('goal.createdAt')}</dt><dd>${esc(formatDate(run.created_at))}</dd></div>
            <div><dt>${tr('goal.updatedAt')}</dt><dd>${esc(formatDate(run.updated_at))}</dd></div>
            <div><dt>${tr('goal.maxDepth')}</dt><dd>${budget ? esc(budget.max_depth) : '—'}</dd></div>
            <div><dt>${tr('goal.maxDescendants')}</dt><dd>${budget ? esc(budget.max_descendants) : '—'}</dd></div>
            <div><dt>${tr('goal.nodes')}</dt><dd>${esc(nodeCount || '—')}</dd></div>
            <div><dt>${tr('goal.template')}</dt><dd class="agd-mono">${esc(run.meta.template || tr('sop.field.none'))}</dd></div>
            <div><dt>${tr('goal.goalSha')}</dt><dd class="agd-mono">${esc(run.meta.goal_sha256 || '—')}</dd></div>
          </dl>
        </section>
      </div>
    </div>
  </div>`;
}

function renderNodeModal(): string {
  if (!modalNodeId || !boot?.tree) return '';
  const node = flatten(boot.tree.node).find((n) => n.job_id === modalNodeId) ?? null;
  if (!node) return '';
  return `
  <div class="agd-goal-modal-backdrop" id="agd-goal-modal-backdrop" role="presentation">
    <div class="agd-goal-modal" role="dialog" aria-modal="true" aria-labelledby="agd-goal-modal-title">
      <div class="agd-goal-modal-head">
        <span id="agd-goal-modal-title">${esc(node.label || node.job_id)}</span>
        <button type="button" class="agd-mini-btn" id="agd-goal-modal-close" title="${tr('chat.cancelDelete')}">✕</button>
      </div>
      <div class="agd-goal-modal-body">
        ${renderNodeInspector(node)}
      </div>
    </div>
  </div>`;
}

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  if (!boot) {
    root.innerHTML = `<div class="agd-sop-loading">…</div>`;
    return;
  }
  const inspectorPart = showInspector
    ? `<div class="agd-sop-splitter" id="agd-goal-inspector-resizer" role="separator" aria-orientation="vertical" title="${tr('sop.resizeInspector')}"></div>${renderDetails()}`
    : '';
  root.innerHTML = `
  <div class="agd-sop is-goal" style="--agd-goal-inspector-width:${boot.inspectorWidth ? `${Math.round(boot.inspectorWidth)}px` : '360px'};--agd-goal-sidebar-width:${Math.round(sidebarWidth)}px">
    ${renderHeader()}
    ${notice ? `<div class="agd-goal-error">${esc(notice)}</div>` : ''}
    <div class="agd-goal-body ${showInspector ? 'has-inspector' : ''} ${sidebarCollapsed ? 'is-sidebar-collapsed' : ''}">
      ${sidebarCollapsed ? '' : renderSidebar()}
      ${sidebarCollapsed ? '' : '<div class="agd-sop-splitter agd-goal-sidebar-resizer" id="agd-goal-sidebar-resizer" role="separator" aria-orientation="vertical"></div>'}
      ${renderTree()}
      ${inspectorPart}
    </div>
    ${renderRunMetaModal()}
    ${renderNodeModal()}
  </div>`;
  wireEvents();
  if (!sidebarCollapsed) {
    wireSidebarResizer();
  }
  if (showInspector) {
    wireInspectorResizer();
  }
}

function renderMainOnly(): void {
  if (modalNodeId || runMetaModalOpen) {
    // When the details modal is open, a partial update would leave it stale.
    render();
    return;
  }
  const main = document.querySelector('.agd-sop-main');
  if (!main || !boot) return;
  main.outerHTML = renderTree();
  if (showInspector) {
    const nextInspector = document.querySelector('.agd-sop-inspector');
    if (nextInspector) nextInspector.outerHTML = renderDetails();
  }
  wireEvents();
  if (showInspector) {
    wireInspectorResizer();
  }
}

function wireEvents(): void {
  if (!viewportListenerWired) {
    viewportListenerWired = true;
    window.addEventListener('resize', () => updateViewport());
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') {
        if (runMetaModalOpen) {
          closeRunMetaModal();
        } else if (modalNodeId) {
          closeNodeModal();
        }
      }
    });
  }
  document.getElementById('agd-goal-sidebar-toggle')?.addEventListener('click', () => {
    void toggleSidebar();
  });
  document.querySelector('[data-mode="chat"]')?.addEventListener('click', () => void bridge.call('openChat'));
  document.querySelector('[data-mode="sop"]')?.addEventListener('click', () => void bridge.call('openSop'));
  document.getElementById('agd-goal-lang')?.addEventListener('click', async () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    render();
    await bridge.call('setLanguage', { language: lang });
  });
  document.getElementById('agd-goal-settings')?.addEventListener('click', () => void bridge.call('openSettings'));
  document.getElementById('agd-goal-refresh')?.addEventListener('click', () => void refreshRuns());
  document.getElementById('agd-goal-overview')?.addEventListener('dblclick', (event) => {
    if ((event.target as HTMLElement).closest('button')) return;
    openRunMetaModal();
  });
  document.getElementById('agd-goal-overview')?.addEventListener('keydown', (event) => {
    const key = (event as KeyboardEvent).key;
    if (key !== 'Enter' && key !== ' ') return;
    if ((event.target as HTMLElement).closest('button')) return;
    event.preventDefault();
    openRunMetaModal();
  });
  document.getElementById('agd-goal-submit')?.addEventListener('click', () => void submitGoal());
  document.getElementById('agd-goal-input')?.addEventListener('input', (event) => {
    draft.goal = (event.target as HTMLTextAreaElement).value;
    const button = document.getElementById('agd-goal-submit') as HTMLButtonElement | null;
    if (button) button.disabled = draft.goal.trim() === '' || busy !== null;
  });
  document.getElementById('agd-goal-identity')?.addEventListener('change', (event) => {
    draft.identityDid = (event.target as HTMLSelectElement).value;
    const identity = boot?.identities.find((item) => item.did === draft.identityDid);
    const preferred = identity?.bindings.find((binding) => binding.is_default)
      ?? identity?.bindings[0];
    if (preferred) {
      if (!draft.provider) draft.provider = preferred.provider;
      if (!draft.daemonId) draft.daemonId = preferred.daemon_id;
    }
    render();
  });
  document.getElementById('agd-goal-provider')?.addEventListener('change', (event) => {
    draft.provider = (event.target as HTMLSelectElement).value;
  });
  document.getElementById('agd-goal-daemon')?.addEventListener('change', (event) => {
    draft.daemonId = (event.target as HTMLSelectElement).value;
  });
  document.getElementById('agd-goal-dir')?.addEventListener('input', (event) => {
    draft.dir = (event.target as HTMLInputElement).value;
  });
  document.getElementById('agd-goal-depth')?.addEventListener('input', (event) => {
    draft.maxDepth = Number((event.target as HTMLInputElement).value) || 5;
  });
  document.getElementById('agd-goal-descendants')?.addEventListener('input', (event) => {
    draft.maxDescendants = Number((event.target as HTMLInputElement).value) || 64;
  });
  document.querySelectorAll<HTMLElement>('[data-run]').forEach((element) => {
    element.addEventListener('click', () => void selectRun(element.dataset.run ?? ''));
  });
  document.querySelectorAll<HTMLElement>('[data-node]').forEach((element) => {
    element.addEventListener('click', () => {
      const jobId = element.dataset.node ?? '';
      if (!jobId) return;
      if (!showInspector) {
        void openNodeModal(jobId);
        return;
      }
      selectedNodeId = jobId;
      nodeLog = null;
      nodeLogJobId = null;
      void loadNodeLog(jobId);
    });
  });
  document.getElementById('agd-goal-log-refresh')?.addEventListener('click', () => {
    if (selectedNodeId) void loadNodeLog(selectedNodeId);
  });
  document.getElementById('agd-goal-modal-backdrop')?.addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeNodeModal();
  });
  document.getElementById('agd-goal-modal-close')?.addEventListener('click', () => closeNodeModal());
  document.getElementById('agd-goal-run-meta-backdrop')?.addEventListener('click', (event) => {
    if (event.target === event.currentTarget) closeRunMetaModal();
  });
  document.getElementById('agd-goal-run-meta-close')?.addEventListener('click', () => closeRunMetaModal());
  document.getElementById('agd-goal-answer-send')?.addEventListener('click', () => {
    if (selectedNodeId) void answerQuestion(selectedNodeId);
  });
  document.getElementById('agd-goal-cancel')?.addEventListener('click', () => void cancelRun());
  document.getElementById('agd-goal-export')?.addEventListener('click', () => void exportSop());
  document.querySelectorAll<HTMLElement>('[data-artifact]').forEach((element) => {
    element.addEventListener('click', async () => {
      const uri = element.dataset.artifact ?? '';
      try {
        await bridge.call('openArtifact', {
          artifactUri: uri,
          dir: null,
          workspaceFolder: boot?.workspaceFolder ?? null,
        });
      } catch (err) {
        notice = String(err instanceof Error ? err.message : err);
        render();
      }
    });
  });
}

async function main(): Promise<void> {
  applyBoot(await bridge.call<GoalBoot>('goal.boot'));
  render();
  startPolling();
  bridge.onEvent((event, payload) => {
    if (event === 'refresh') {
      applyBoot(payload as GoalBoot);
      render();
      startPolling();
    } else if (event === 'language') {
      lang = payload as UiLanguage;
      render();
    }
  });
}

void main();
