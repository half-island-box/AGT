import './mode-switcher.css';
import './markdown.css';
import './sop-editor.css';
import { renderMarkdown } from './markdown';
import { t, type MessageKey } from '../core/i18n';
import { issues as localIssues, wouldCycle } from '../core/sop/graph';
import { computeLayout, type SopPoint } from '../core/sop/layout';
import { buildOptions, renderOptions } from '../core/sop/options';
import type { EditIntent, SopDoc, SopIssue, SopNode } from '../core/sop/model';
import type {
  ServerState,
  SopNodeLog,
  SopStatus,
  UiLanguage,
} from '../core/types';
import { SopCanvas } from './sop-canvas';
import { bridge } from './bridge';

interface SopTemplateInfo {
  path: string;
  label: string;
  nodeCount: number;
}

interface SopDaemonOption {
  id: string;
  label: string;
  online: boolean;
  providers: string[];
}

interface SopBoot {
  language: UiLanguage;
  state: ServerState;
  templatePath: string | null;
  templates: SopTemplateInfo[];
  daemons: SopDaemonOption[];
  model: SopDoc | null;
  text: string;
  dirty: boolean;
  positions: Record<string, SopPoint>;
  parseError: string | null;
  workspaceFolder: string | null;
  localDaemonId: string | null;
  inspectorWidth: number | null;
  trackedRun: SopRunSummary | null;
  runs: SopRunSummary[];
  run: SopRunSummary | null;
}

interface SopRunSummary {
  rootJobId: string;
  startedAt: number;
  stale: boolean;
}

interface ServerValidation {
  ok: boolean;
  error: string | null;
  /** The host could not reach the server, so no verdict is available. */
  offline?: boolean;
  /** The running server does not expose /api/sop/validate (older build). */
  stale?: boolean;
  at: number;
}

type NodeField = 'name' | 'identity' | 'input' | 'output' | 'provider' | 'daemon' | 'dir';

let boot: SopBoot | null = null;
let lang: UiLanguage = 'zh';
let selected: string | null = null;
let positions: Record<string, SopPoint> = {};
let validation: ServerValidation | null = null;
let busy: 'save' | 'validate' | null = null;
let toast: { text: string; kind: 'info' | 'error' } | null = null;
let showPreview = false;
let pendingImpact:
  | { kind: 'deleteNode'; name: string; dependents: string[]; placeholders: string[] }
  | null = null;
let canvas: SopCanvas | null = null;
let view: { scale: number; offset: SopPoint } | undefined;
let viewInitialized = false;
let layoutTimer: number | null = null;
let newIdentityAlias: string | null = null;
let runStatus: SopStatus | null = null;
let selectedRunStatus: SopStatus | null = null;
let selectedRunStatusRoot: string | null = null;
let runBusy: 'run' | 'cancel' | null = null;
let nodeLog: SopNodeLog | null = null;
let nodeLogFor: string | null = null;
let pollTimer: number | null = null;
let editorMode: 'edit' | 'inspect' = 'edit';

function runActive(): boolean {
  return boot?.trackedRun !== null && boot?.trackedRun !== undefined;
}

function inspectRunRoot(): string | null {
  return boot?.run?.rootJobId ?? null;
}

function runRunning(): boolean {
  return runActive() && !(runStatus?.all_terminal ?? false);
}

function statusOf(name: string): string {
  return runStatus?.nodes.find((node) => node.name === name)?.status ?? '';
}

function selectedStatusOf(name: string): string {
  return selectedRunStatus?.nodes.find((node) => node.name === name)?.status ?? '';
}

function formatRunTime(startedAt: number): string {
  return new Date(startedAt).toLocaleString();
}

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

function model(): SopDoc | null {
  return boot?.model ?? null;
}

function selectedNode(): SopNode | null {
  return model()?.nodes.find((node) => node.name === selected) ?? null;
}

function nodeIssues(node: SopNode): SopIssue[] {
  return allIssues().filter((issue) => issue.node === node.name);
}

function allIssues(): SopIssue[] {
  const doc = model();
  if (!doc) {
    return [];
  }
  const list = localIssues(doc);
  if (validation && !validation.ok) {
    const detail = validation.offline
      ? tr('sop.serverOffline')
      : validation.stale
        ? tr('sop.validateStale')
        : validation.error ?? tr('sop.validFail');
    list.push({ level: 'error', message: `${tr('sop.sourceServer')}: ${detail}` });
  }
  return list;
}

function applyBoot(next: SopBoot): void {
  boot = next;
  lang = next.language;
  if (!next.trackedRun) {
    runStatus = null;
    stopPolling();
  }
  selectedRunStatusRoot = null;
  // Local drags win over the values echoed back by the host (workspace
  // state is written on a debounce, so its copy can lag a drag).
  const merged: Record<string, SopPoint> = {};
  const computed = next.model ? computeLayout(next.model) : {};
  for (const node of next.model?.nodes ?? []) {
    merged[node.name] = positions[node.name] ?? next.positions[node.name] ?? computed[node.name];
  }
  positions = merged;
  if (selected && !next.model?.nodes.some((node) => node.name === selected)) {
    selected = null;
  }
  if (runActive()) {
    startPolling();
  }
}

/* ── run monitoring ────────────────────────────────────────────────── */

function startPolling(): void {
  if (pollTimer !== null || !runActive()) {
    return;
  }
  const tick = async (): Promise<void> => {
    pollTimer = null;
    if (!runActive()) {
      return;
    }
    const wasTerminal = runStatus?.all_terminal ?? false;
    try {
      runStatus = await bridge.call<SopStatus>('sop.status', {
        rootJobId: boot?.trackedRun?.rootJobId,
      });
    } catch (err) {
      flash(String(err instanceof Error ? err.message : err), 'error');
      return;
    }
    // Reaching the terminal state changes the *chrome* too (the canvas
    // becomes editable again, Run replaces Cancel), so that transition takes
    // a full re-render; ordinary progress stays a partial update.
    if (runStatus.all_terminal && !wasTerminal) {
      render();
    } else {
      refreshRunUI();
    }
    if (!runStatus.all_terminal) {
      pollTimer = window.setTimeout(() => void tick(), 1500);
    } else if (selected) {
      await loadNodeLog(selected);
    }
  };
  pollTimer = window.setTimeout(() => void tick(), 400);
}

function stopPolling(): void {
  if (pollTimer !== null) {
    window.clearTimeout(pollTimer);
    pollTimer = null;
  }
}

function clampInspectorWidth(width: number, containerWidth: number): number {
  const max = Math.max(280, containerWidth - 248 - 320);
  return Math.max(260, Math.min(max, width));
}

function applyInspectorWidth(width: number | null): void {
  const root = document.querySelector<HTMLElement>('.agd-sop');
  if (root && width !== null) {
    root.style.setProperty('--agd-sop-inspector-width', `${Math.round(width)}px`);
  }
}

function wireInspectorResizer(): void {
  const handle = document.getElementById('agd-sop-inspector-resizer');
  const container = document.querySelector<HTMLElement>('.agd-sop-body');
  if (!handle || !container) {
    return;
  }

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) {
      return;
    }
    event.preventDefault();
    container.classList.add('is-resizing');
    handle.setPointerCapture(event.pointerId);

    const move = (moveEvent: PointerEvent): void => {
      const rect = container.getBoundingClientRect();
      const width = clampInspectorWidth(rect.right - moveEvent.clientX, rect.width);
      if (boot) {
        boot.inspectorWidth = width;
      }
      applyInspectorWidth(width);
    };

    const finish = (): void => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      container.classList.remove('is-resizing');
      if (boot?.inspectorWidth !== null && boot?.inspectorWidth !== undefined) {
        void bridge.call('sop.setInspectorWidth', { width: boot.inspectorWidth });
      }
    };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  });
}

/**
 * Update only what a status change touches (node classes, banner, log) —
 * a full re-render would tear down the canvas on every poll.
 */
function refreshRunUI(): void {
  for (const element of document.querySelectorAll<HTMLElement>('.agd-sop-node')) {
    const name = element.dataset.node ?? '';
    const status = statusOf(name);
    for (const cls of [...element.classList]) {
      if (cls.startsWith('is-') && !['is-selected', 'is-readonly', 'is-drop'].includes(cls)) {
        element.classList.remove(cls);
      }
    }
    if (status !== '') {
      element.classList.add(`is-${status}`);
    }
    element.dataset.status = status;
  }
  const banner = document.getElementById('agd-sop-run-banner');
  if (banner) {
    banner.innerHTML = renderRunBanner();
    wireRunBanner();
  }
  refreshSelectionUI();
}

async function loadNodeLog(name: string): Promise<void> {
  const rootJobId = inspectRunRoot();
  if (!rootJobId || name === '') {
    nodeLog = null;
    nodeLogFor = null;
    selectedRunStatus = null;
    selectedRunStatusRoot = null;
    return;
  }
  if (selectedRunStatusRoot !== rootJobId) {
    selectedRunStatus = await bridge.call<SopStatus>('sop.status', { rootJobId });
    selectedRunStatusRoot = rootJobId;
  }
  try {
    nodeLog = await bridge.call<SopNodeLog>('sop.nodeLog', {
      rootJobId,
      node: name,
    });
    nodeLogFor = name;
  } catch {
    nodeLog = null;
    nodeLogFor = null;
  }
  refreshSelectionUI();
}

async function selectRun(rootJobId: string): Promise<void> {
  if (!rootJobId || rootJobId === inspectRunRoot()) {
    return;
  }
  selectedRunStatus = null;
  selectedRunStatusRoot = null;
  nodeLog = null;
  nodeLogFor = null;
  await call('sop.selectRun', { rootJobId });
  if (selected) {
    await loadNodeLog(selected);
    render();
  }
}

function flash(text: string, kind: 'info' | 'error' = 'info'): void {
  toast = { text, kind };
  render();
  window.setTimeout(() => {
    toast = null;
    render();
  }, 2400);
}

/**
 * Selection changes must NOT rebuild the canvas: the pointer is still down
 * while dragging a node, and re-rendering would replace the element the
 * drag is attached to. Only the highlight and the inspector are refreshed.
 */
function refreshSelectionUI(): void {
  for (const element of document.querySelectorAll<HTMLElement>('.agd-sop-node')) {
    element.classList.toggle('is-selected', element.dataset.node === selected);
  }
  const inspector = document.querySelector('.agd-sop-inspector');
  if (inspector) {
    inspector.innerHTML = renderInspector();
    wireInspector();
  }
}

async function call(method: string, params?: Record<string, unknown>): Promise<void> {
  try {
    const result = await bridge.call<SopBoot>(method, params);
    if (result && typeof result === 'object' && 'templates' in result) {
      applyBoot(result);
    }
    render();
  } catch (err) {
    flash(String(err instanceof Error ? err.message : err), 'error');
  }
}

async function apply(intent: EditIntent): Promise<boolean> {
  try {
    const result = await bridge.call<SopBoot>('sop.apply', { intent });
    applyBoot(result);
    render();
    return true;
  } catch (err) {
    flash(String(err instanceof Error ? err.message : err), 'error');
    return false;
  }
}

/** Ask the server to validate the document (the DSL authority). */
async function runValidate(): Promise<void> {
  busy = 'validate';
  render();
  try {
    const result = await bridge.call<ServerValidation>('sop.validate');
    validation = { ...result, at: Date.now() };
  } catch (err) {
    validation = {
      ok: false,
      error: String(err instanceof Error ? err.message : err),
      at: Date.now(),
    };
  } finally {
    busy = null;
    render();
  }
}

/** Submit the document, then start tracking the run. */
async function startRun(): Promise<void> {
  if (runBusy) {
    return;
  }
  const problems = localIssues(model() ?? { name: '', identities: [], nodes: [] });
  if (problems.some((issue) => issue.level === 'error')) {
    flash(tr('sop.runBlocked'), 'error');
    return;
  }
  runBusy = 'run';
  render();
  try {
    const payload = await bridge.call<SopBoot>('sop.run');
    applyBoot(payload);
    runStatus = null;
    flash(tr('sop.runSubmitted'));
  } catch (err) {
    const message = String(err instanceof Error ? err.message : err);
    flash(message.includes('no-document') ? tr('sop.noDocument') : message, 'error');
  } finally {
    runBusy = null;
    render();
    if (runActive()) {
      startPolling();
      if (selected) {
        void loadNodeLog(selected);
      }
    }
  }
}

async function cancelRun(): Promise<void> {
  if (runBusy) {
    return;
  }
  runBusy = 'cancel';
  render();
  try {
    await bridge.call('sop.cancelRun', { rootJobId: boot?.trackedRun?.rootJobId });
    flash(tr('sop.cancelRequested'));
  } catch (err) {
    flash(String(err instanceof Error ? err.message : err), 'error');
  } finally {
    runBusy = null;
    render();
    startPolling();
  }
}

function wireRunBanner(): void {
  document.getElementById('agd-sop-copy-root')?.addEventListener('click', async () => {
    const id = boot?.trackedRun?.rootJobId;
    if (!id) {
      return;
    }
    try {
      await navigator.clipboard?.writeText(id);
      flash(tr('settings.copied'));
    } catch {
      flash(id);
    }
  });
  document.getElementById('agd-sop-forget-run')?.addEventListener('click', async () => {
    stopPolling();
    runStatus = null;
    await call('sop.forgetRun');
  });
}

function persistLayout(next: Record<string, SopPoint>): void {
  positions = next;
  if (layoutTimer !== null) {
    window.clearTimeout(layoutTimer);
  }
  layoutTimer = window.setTimeout(() => {
    void bridge.call('sop.setLayout', { positions });
  }, 400);
}

/* ── rendering ─────────────────────────────────────────────────────── */

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) {
    return;
  }
  if (!boot) {
    root.innerHTML = `<div class="agd-sop-loading">…</div>`;
    return;
  }
  applyInspectorWidth(boot.inspectorWidth);
  const doc = model();
  if (!doc) {
    // No template bound yet: guide instead of showing dead chrome. The
    // toolbar stays rendered but disabled (see renderToolbar).
    root.innerHTML = `
    <div class="agd-sop is-empty">
      <header class="agd-sop-head">
        <div class="agd-sop-brand"><span>AGT</span><small>${tr('sop.title')}</small></div>
        <nav class="agd-modes" aria-label="AGT modes">
        <button type="button" class="agd-mode" data-mode="chat">${tr('mode.chat')}</button>
        <button type="button" class="agd-mode agd-mode-active" data-mode="sop" aria-current="page">${tr('mode.sop')}</button>
        <button type="button" class="agd-mode" data-mode="goal">${tr('mode.goal')}</button>
        </nav>
        <div class="agd-sop-tools">
          <button type="button" class="agd-icon-btn" id="agd-sop-lang">${tr('settings.langToggle')}</button>
          <button type="button" class="agd-icon-btn" id="agd-sop-settings" title="${tr('chat.openSettings')}">⚙</button>
        </div>
      </header>
      <div class="agd-sop-body">
        <aside class="agd-sop-side">${renderTemplates()}</aside>
        <section class="agd-sop-main">
          <div class="agd-sop-empty-state">
            <h2>${tr('sop.emptyTitle')}</h2>
            <p>${tr('sop.emptyHint')}</p>
            <button type="button" class="agd-btn agd-btn-primary" id="agd-sop-new">${tr('sop.newSop')}</button>
          </div>
        </section>
        <aside class="agd-sop-inspector"></aside>
      </div>
      ${toast ? `<div class="agd-sop-toast ${toast.kind === 'error' ? 'is-error' : ''}">${esc(toast.text)}</div>` : ''}
    </div>`;
    wireEvents();
    return;
  }
  root.innerHTML = `
    <div class="agd-sop ${editorMode === 'inspect' ? 'is-inspect' : ''}" style="--agd-sop-inspector-width:${boot.inspectorWidth ? `${Math.round(boot.inspectorWidth)}px` : '288px'}">
    <header class="agd-sop-head">
      <div class="agd-sop-brand"><span>AGT</span><small>${tr('sop.title')}</small></div>
      <nav class="agd-modes" aria-label="AGT modes">
        <button type="button" class="agd-mode" data-mode="chat">${tr('mode.chat')}</button>
        <button type="button" class="agd-mode agd-mode-active" data-mode="sop" aria-current="page">${tr('mode.sop')}</button>
        <button type="button" class="agd-mode" data-mode="goal">${tr('mode.goal')}</button>
      </nav>
      <div class="agd-sop-tools">
        <span class="agd-sop-doc">${doc ? esc(boot.templatePath ?? '') : ''}</span>
        <span class="agd-sop-dirty">${boot.dirty ? tr('sop.unsaved') : ''}</span>
        <button type="button" class="agd-icon-btn" id="agd-sop-lang">${tr('settings.langToggle')}</button>
        <button type="button" class="agd-icon-btn" id="agd-sop-settings" title="${tr('chat.openSettings')}">⚙</button>
      </div>
    </header>
    <div class="agd-sop-body">
      <aside class="agd-sop-side">
        ${renderTemplates()}
        ${renderIdentities(doc)}
      </aside>
      <section class="agd-sop-main">
        ${renderToolbar()}
        <div class="agd-sop-canvas" id="agd-sop-canvas"></div>
        ${showPreview ? renderPreview() : ''}
        ${renderProblems()}
      </section>
      <div class="agd-sop-splitter" id="agd-sop-inspector-resizer" role="separator" aria-orientation="vertical" title="${tr('sop.resizeInspector')}"></div>
      <aside class="agd-sop-inspector">
        ${renderInspector()}
      </aside>
    </div>
    ${runActive() ? renderRunBanner() : ''}
    ${toast ? `<div class="agd-sop-toast ${toast.kind === 'error' ? 'is-error' : ''}">${esc(toast.text)}</div>` : ''}
    ${pendingImpact ? renderImpact() : ''}
  </div>`;

  mountCanvas();
  wireEvents();
  wireInspector();
  wireInspectorResizer();
  wireRunBanner();
}

function renderTemplates(): string {
  const templates = boot?.templates ?? [];
  const rows = templates
    .map(
      (template) => `
      <button type="button" class="agd-sop-template ${template.path === boot?.templatePath ? 'is-active' : ''}" data-template="${esc(template.path)}">
        <span>${esc(template.label)}</span>
        <small>${tr('sop.nodeCount', { count: template.nodeCount })}</small>
      </button>`,
    )
    .join('');
  return `
  <div class="agd-sop-card">
    <div class="agd-sop-card-head">
      <span>${tr('sop.templates')}</span>
      <span class="agd-flex"></span>
      <button type="button" class="agd-mini-btn" id="agd-sop-new" title="${tr('sop.newSop')}">+</button>
    </div>
    ${rows || `<p class="agd-sop-empty">${tr('sop.noTemplates')}</p>`}
  </div>`;
}

function renderRunHistory(): string {
  const runs = boot?.runs ?? [];
  if (runs.length === 0) {
    return `
    <div class="agd-sop-card agd-sop-runs-card">
      <div class="agd-sop-card-head"><span>${tr('sop.runHistory')}</span></div>
      <p class="agd-sop-empty agd-dim">${tr('sop.noRuns')}</p>
    </div>`;
  }
  return `
  <div class="agd-sop-card agd-sop-runs-card">
    <div class="agd-sop-card-head"><span>${tr('sop.runHistory')}</span></div>
    <div class="agd-sop-runs">${runs
      .map(
        (run) => `
        <button type="button" class="agd-sop-run-item ${run.rootJobId === boot?.run?.rootJobId ? 'is-active' : ''}" data-run="${esc(run.rootJobId)}" title="${esc(run.rootJobId)}">
          <span class="agd-mono">${esc(run.rootJobId.slice(0, 8))}</span>
          <small>${esc(formatRunTime(run.startedAt))}</small>
          ${run.stale ? `<em>${tr('sop.runStale')}</em>` : ''}
        </button>`,
      )
      .join('')}</div>
  </div>`;
}

function renderIdentities(doc: SopDoc | null): string {
  if (!doc) {
    return '';
  }
  const rows = doc.identities
    .map(
      (identity) => `
      <div class="agd-sop-identity" data-alias="${esc(identity.alias)}">
        <div class="agd-sop-identity-head">
          <span class="agd-mono">${esc(identity.alias)}</span>
          <button type="button" class="agd-mini-btn" data-identity-delete="${esc(identity.alias)}">×</button>
        </div>
        <label>${tr('sop.identityDid')}<input class="agd-input agd-mono" data-identity-field="did" data-alias="${esc(identity.alias)}" value="${esc(identity.did)}"></label>
        <label>${tr('sop.identityProvider')}<input class="agd-input" data-identity-field="provider" data-alias="${esc(identity.alias)}" value="${esc(identity.provider)}"></label>
      </div>`,
    )
    .join('');
  return `
  <div class="agd-sop-card">
    <div class="agd-sop-card-head">
      <span>${tr('sop.identities')}</span>
      <span class="agd-flex"></span>
      <button type="button" class="agd-mini-btn" id="agd-sop-identity-add" title="${tr('sop.addIdentity')}">+</button>
    </div>
    ${rows || `<p class="agd-sop-empty">—</p>`}
    ${
      newIdentityAlias !== null
        ? `<div class="agd-sop-identity is-new">
             <input class="agd-input agd-mono" id="agd-sop-identity-new" placeholder="${tr('sop.identityAlias')}" value="${esc(newIdentityAlias)}">
           </div>`
        : ''
    }
  </div>`;
}

function renderToolbar(): string {
  const doc = model();
  const running = runRunning();
  // In inspect mode all editing controls are hidden; Run/Cancel are only
  // shown here too (edit mode focuses on template authoring).
  const editable = editorMode === 'edit' && doc !== null && boot?.parseError === null && !running;
  const done = runActive() && !running;

  const modeToggle = `
  <span class="agd-sop-mode-toggle">
    <button type="button" class="agd-sop-mode-btn ${editorMode === 'edit' ? 'is-active' : ''}" data-editor-mode="edit" title="${tr('sop.modeEdit')}">✏ ${tr('sop.modeEdit')}</button>
    <button type="button" class="agd-sop-mode-btn ${editorMode === 'inspect' ? 'is-active' : ''}" data-editor-mode="inspect" title="${tr('sop.modeInspect')}">👁 ${tr('sop.modeInspect')}</button>
  </span>`;

  if (editorMode === 'inspect') {
    return `
    <div class="agd-sop-toolbar">
      ${modeToggle}
      <span class="agd-flex"></span>
      <button type="button" class="agd-btn" id="agd-sop-fit">${tr('sop.fit')}</button>
      <button type="button" class="agd-btn" id="agd-sop-zoom-out">−</button>
      <button type="button" class="agd-btn" id="agd-sop-zoom-in">+</button>
      <span class="agd-flex"></span>
      ${
        running
          ? `<button type="button" class="agd-btn" id="agd-sop-cancel-run" ${runBusy ? 'disabled' : ''}>${runBusy === 'cancel' ? tr('sop.cancelling') : tr('sop.cancelRun')}</button>`
          : `<button type="button" class="agd-btn agd-btn-primary" id="agd-sop-run" ${doc !== null && boot?.parseError === null && !runBusy ? '' : 'disabled'}>${runBusy === 'run' ? tr('sop.running') : done ? tr('sop.runAgain') : tr('sop.run')}</button>`
      }
    </div>`;
  }

  return `
  <div class="agd-sop-toolbar">
    ${modeToggle}
    <span class="agd-sop-toolbar-sep"></span>
    <button type="button" class="agd-btn" id="agd-sop-add" ${editable ? '' : 'disabled'}>${tr('sop.addNode')}</button>
    <button type="button" class="agd-btn" id="agd-sop-layout" ${editable ? '' : 'disabled'}>${tr('sop.autoLayout')}</button>
    <button type="button" class="agd-btn" id="agd-sop-fit">${tr('sop.fit')}</button>
    <button type="button" class="agd-btn" id="agd-sop-zoom-out">−</button>
    <button type="button" class="agd-btn" id="agd-sop-zoom-in">+</button>
    <span class="agd-flex"></span>
    <button type="button" class="agd-btn" id="agd-sop-preview">${showPreview ? '▾ ' : '▸ '}${tr('sop.preview')}</button>
    <button type="button" class="agd-btn" id="agd-sop-yaml">${tr('sop.openYaml')}</button>
    <button type="button" class="agd-btn" id="agd-sop-validate" ${busy || !editable ? 'disabled' : ''}>${busy === 'validate' ? tr('sop.validating') : tr('sop.validate')}</button>
    ${
      running
        ? `<button type="button" class="agd-btn" id="agd-sop-cancel-run" ${runBusy ? 'disabled' : ''}>${runBusy === 'cancel' ? tr('sop.cancelling') : tr('sop.cancelRun')}</button>`
        : `<button type="button" class="agd-btn agd-btn-primary" id="agd-sop-run" ${doc !== null && boot?.parseError === null && !running && !runBusy ? '' : 'disabled'}>${runBusy === 'run' ? tr('sop.running') : done ? tr('sop.runAgain') : tr('sop.run')}</button>`
    }
    <button type="button" class="agd-btn agd-btn-primary" id="agd-sop-save" ${busy || !boot?.dirty ? 'disabled' : ''}>${busy === 'save' ? '…' : tr('sop.save')}</button>
  </div>`;
}

function renderRunBanner(): string {
  const run = boot?.trackedRun;
  if (!run) {
    return '';
  }
  const statuses = runStatus?.nodes ?? [];
  const done = statuses.filter((n) => n.status === 'done').length;
  const failed = statuses.filter((n) => n.status === 'failed').length;
  const phase = !runStatus
    ? tr('sop.loadingRun')
    : runStatus.all_terminal
      ? failed > 0
        ? tr('sop.runFailed', { failed })
        : tr('sop.runDone')
      : tr('sop.runProgress', { done, total: statuses.length });
  return `
  <div class="agd-sop-run-banner" id="agd-sop-run-banner">
    <span class="agd-sop-run-phase">${esc(phase)}</span>
    <code class="agd-mono">${esc(run.rootJobId)}</code>
    <button type="button" class="agd-mini-btn" id="agd-sop-copy-root">${tr('settings.copy')}</button>
    ${run.stale ? `<span class="agd-sop-run-stale">${tr('sop.runStale')}</span>` : ''}
    <span class="agd-flex"></span>
    <button type="button" class="agd-mini-btn" id="agd-sop-forget-run">${tr('sop.forgetRun')}</button>
  </div>`;
}

function renderPreview(): string {
  return `<pre class="agd-sop-preview"><code>${esc(boot?.text ?? '')}</code></pre>`;
}

function renderProblems(): string {
  const list = allIssues();
  if (list.length === 0) {
    return `<div class="agd-sop-problems"><div class="agd-sop-card-head"><span>${tr('sop.problems')}</span><span class="agd-flex"></span><span class="agd-sop-ok">${tr('sop.noProblems')}</span></div></div>`;
  }
  const rows = list
    .map(
      (issue) => `
      <button type="button" class="agd-sop-problem is-${issue.level}" ${issue.node ? `data-problem-node="${esc(issue.node)}"` : ''}>
        <span class="agd-sop-level">${issue.level === 'error' ? tr('sop.level.error') : issue.level === 'warning' ? tr('sop.level.warning') : tr('sop.level.info')}</span>
        <span>${esc(issue.message)}</span>
      </button>`,
    )
    .join('');
  return `
  <div class="agd-sop-problems">
    <div class="agd-sop-card-head">
      <span>${tr('sop.problems')}</span>
      <span class="agd-flex"></span>
      <small class="agd-dim">${validation ? (validation.ok ? tr('sop.validOk') : tr('sop.validFail')) : tr('sop.sourceLocal')}</small>
    </div>
    ${
      validation?.stale
        ? `<div class="agd-sop-stale"><button type="button" class="agd-btn agd-btn-primary" id="agd-sop-restart-server">${tr('sop.restartServer')}</button></div>`
        : ''
    }
    ${rows}
  </div>`;
}

function renderInspector(): string {
  const node = selectedNode();

  /* ── inspect mode: log panel replaces the edit form ─────────────── */
  if (editorMode === 'inspect') {
    if (!node) {
      return `
      ${renderRunHistory()}
      <div class="agd-sop-card agd-sop-log-card">
        <div class="agd-sop-card-head"><span>${tr('sop.modeInspect')}</span></div>
        <p class="agd-sop-empty">${tr('sop.noSelection')}</p>
      </div>`;
    }
    if (!runActive()) {
      if (boot?.run) {
        return renderInspectLog(node);
      }
      return `
      <div class="agd-sop-card agd-sop-log-card">
        <div class="agd-sop-card-head"><span>${esc(node.name)}</span></div>
        <p class="agd-sop-empty agd-dim">${tr('sop.inspectNoRun')}</p>
      </div>`;
    }
    return renderInspectLog(node);
  }

  /* ── edit mode: form only (no execution log) ────────────────────── */
  if (!node) {
    return `
    <div class="agd-sop-card">
      <div class="agd-sop-card-head"><span>${tr('sop.inspector')}</span></div>
      <p class="agd-sop-empty">${tr('sop.noSelection')}</p>
    </div>`;
  }
  const doc = model() as SopDoc;
  // Always display the value the document holds — an alias that was deleted
  // from `identities` must still show up (as the current value) instead of
  // silently collapsing to 「不指定」.
  const identityOptions = renderOptions(
    buildOptions({
      values: doc.identities.map((identity) => identity.alias),
      current: node.identity,
      noneLabel: tr('sop.field.none'),
      unmatchedLabel: (alias) => tr('sop.identityCurrent', { name: alias }),
    }),
    esc,
  );
  const candidates = doc.nodes.filter((other) => other.name !== node.name);
  const deps = node.dependsOn
    .map(
      (dep) => `
      <span class="agd-sop-dep">${esc(dep)}<button type="button" class="agd-mini-btn" data-dep-remove="${esc(dep)}">×</button></span>`,
    )
    .join('') || `<span class="agd-dim">—</span>`;
  const addable = candidates.filter((other) => !node.dependsOn.includes(other.name));
  const addOptions = [
    `<option value="">${tr('sop.addDependency')}</option>`,
    ...addable.map((other) => `<option value="${esc(other.name)}">${esc(other.name)}</option>`),
  ].join('');
  const selectedDaemon = (boot?.daemons ?? []).find((d) => d.id === node.daemon);

  // Daemon pinning is validated server-side at submit
  // (``_validate_explicit_targets``): a pinned daemon needs a provider, and
  // that provider must have a live runtime on it. Surface it here instead of
  // letting the user find out when the run is rejected.
  const pinProblems: SopIssue[] = [];
  if (node.daemon !== '' && node.provider === '') {
    pinProblems.push({ level: 'warning', message: tr('sop.pinNeedsProvider') });
  } else if (
    node.daemon !== '' &&
    node.provider !== '' &&
    selectedDaemon &&
    !selectedDaemon.providers.includes(node.provider)
  ) {
    pinProblems.push({
      level: 'warning',
      message: tr('sop.pinProviderMissing', {
        daemon: selectedDaemon.label,
        name: node.provider,
      }),
    });
  }
  const nodeProblems = [...nodeIssues(node), ...pinProblems];

  return `
  <div class="agd-sop-card">
    <div class="agd-sop-card-head">
      <span>${tr('sop.inspector')}</span>
      <span class="agd-flex"></span>
      <button type="button" class="agd-mini-btn is-danger" id="agd-sop-delete-node" title="${tr('sop.deleteNode')}">🗑</button>
    </div>
    ${
      nodeProblems.length > 0
        ? `<div class="agd-sop-node-problems">${nodeProblems
            .map((issue) => `<p class="is-${issue.level}">${esc(issue.message)}</p>`)
            .join('')}</div>`
        : ''
    }
    <label class="agd-sop-field">${tr('sop.field.name')}<input class="agd-input agd-mono" id="agd-sop-node-name" value="${esc(node.name)}"></label>
    <label class="agd-sop-field">${tr('sop.field.identity')}<select class="agd-input" id="agd-sop-node-identity">${identityOptions}</select></label>
    <label class="agd-sop-field">${tr('sop.field.input')}<textarea class="agd-input agd-sop-textarea" id="agd-sop-node-input" rows="5" placeholder="${tr('sop.field.inputPh')}">${esc(node.input)}</textarea></label>
    <label class="agd-sop-field">${tr('sop.field.output')}<input class="agd-input agd-mono" id="agd-sop-node-output" value="${esc(node.output)}"></label>
    <label class="agd-sop-field">
      <span>${tr('sop.field.daemon')}</span>
      <select class="agd-input agd-mono" id="agd-sop-node-daemon">${daemonOptions(node)}</select>
      <small class="agd-dim">${selectedDaemon ? esc(selectedDaemon.id) : ''}</small>
    </label>
    <label class="agd-sop-field">
      <span>${tr('sop.field.provider')}</span>
      <select class="agd-input agd-mono" id="agd-sop-node-provider">${providerOptions(node)}</select>
      <small class="agd-dim">${pinHint(node)}</small>
    </label>
    <label class="agd-sop-field">${tr('sop.field.dir')}<input class="agd-input agd-mono" id="agd-sop-node-dir" value="${esc(node.dir)}"></label>
    <div class="agd-sop-field">
      <span>${tr('sop.dependencies')}</span>
    <div class="agd-sop-deps">${deps}</div>
    <select class="agd-input" id="agd-sop-dep-add">${addOptions}</select>
    </div>
  </div>`;
}

/** Execution log for the selected node (only once a run is tracked). */
/**
 * Full-height execution log for inspect mode. Renders in the wide
 * right-hand inspector panel; the host polls run status, and each poll
 * calls ``refreshSelectionUI()`` which re-loads the log for the
 * currently selected node.
 */
function renderInspectLog(node: SopNode): string {
  const status = selectedStatusOf(node.name);
  const log = nodeLogFor === node.name ? nodeLog : null;
  const steps = (log?.steps ?? [])
    .map(
      (step) => `
      <div class="agd-sop-step">
        <div class="agd-sop-step-head">
          <span class="agd-mono">${esc(step.task_id.slice(0, 8))}</span>
          <span class="agd-dim">${esc(step.status)}${step.exit_code === null ? '' : ` · exit ${step.exit_code}`}</span>
        </div>
        ${step.stdout ? `<div class="agd-sop-stdout agd-md">${renderMarkdown(step.stdout)}</div>` : ''}
        ${step.stderr ? `<div class="agd-sop-stderr agd-md">${renderMarkdown(step.stderr)}</div>` : ''}
      </div>`,
    )
    .join('');
  const artifacts = selectedRunStatus?.nodes.find((n) => n.name === node.name)
    ?.artifact_details ?? [];
  return `
  ${renderRunHistory()}
  <div class="agd-sop-card agd-sop-log-card">
    <div class="agd-sop-card-head">
      <span>${esc(node.name)}</span>
      ${status ? `<em class="agd-sop-run-status is-${esc(status)}">${esc(statusText(status))}</em>` : ''}
    </div>
    ${
      artifacts.length > 0
        ? `<div class="agd-sop-artifacts">${artifacts
            .map(
              (artifact) =>
                `<button type="button" class="agd-sop-artifact" data-artifact="${esc(artifact.uri)}" title="${tr('sop.artifactOpen')}">
                   <strong>${esc(artifact.name)}</strong>
                   <small class="agd-mono">${esc(artifact.uri)}</small>
                 </button>`,
            )
            .join('')}</div>`
        : ''
    }
    <div class="agd-sop-steps agd-sop-steps-lg">${
      steps ||
      `<p class="agd-dim">${
        status === '' || status === 'blocked' ? tr('sop.nodeLogPending') : tr('sop.nodeLogEmpty')
      }</p>`
    }</div>
  </div>`;
}

function statusText(status: string): string {
  const key = `sop.status.${status}` as MessageKey;
  const text = tr(key);
  return text === key ? status : text;
}

function renderImpact(): string {
  const lines: string[] = [];
  if (pendingImpact && pendingImpact.dependents.length > 0) {
    lines.push(tr('sop.impact.dependents', { nodes: pendingImpact.dependents.join(', ') }));
  }
  if (pendingImpact && pendingImpact.placeholders.length > 0) {
    lines.push(
      tr('sop.impact.placeholders', { nodes: pendingImpact.placeholders.join(', ') }),
    );
  }
  return `
  <div class="agd-sop-modal">
    <div class="agd-sop-modal-body">
      <h3>${tr('sop.impact.title')}</h3>
      ${lines.map((line) => `<p>${esc(line)}</p>`).join('') || '<p>—</p>'}
      <div class="agd-actions">
        <button type="button" class="agd-btn agd-btn-primary" id="agd-sop-impact-confirm">${tr('sop.impact.confirm')}</button>
        <button type="button" class="agd-btn" id="agd-sop-impact-cancel">${tr('sop.impact.cancel')}</button>
      </div>
    </div>
  </div>`;
}

/** Daemon select: value is the ``daemon_id`` the DSL expects. */
function daemonOptions(node: SopNode): string {
  const daemons = boot?.daemons ?? [];
  const options = buildOptions({
    values: daemons.map((daemon) => daemon.id),
    current: node.daemon,
    noneLabel: daemons.length === 0 ? tr('sop.noDaemons') : tr('sop.field.none'),
    unmatchedLabel: (id) => tr('sop.daemonCurrent', { name: id }),
  });
  // Friendly labels (display name + offline marker) on top of the shared
  // option list, which owns the selected/current-value semantics.
  const byId = new Map(daemons.map((daemon) => [daemon.id, daemon]));
  return options
    .map((option) => {
      const daemon = byId.get(option.value);
      const label = daemon
        ? `${daemon.label}${daemon.online ? '' : ` · ${tr('sop.daemonOffline')}`}`
        : option.label;
      return `<option value="${esc(option.value)}"${option.selected ? ' selected' : ''}>${esc(label)}</option>`;
    })
    .join('');
}

/**
 * Provider select, scoped to the pinned daemon: choosing a daemon narrows
 * the list to the providers that daemon actually registered. Without a
 * pin the list is the union across daemons, and the current value is always
 * kept selectable so an offline provider is never silently dropped.
 */
function providerOptions(node: SopNode): string {
  const daemons = boot?.daemons ?? [];
  const selectedDaemon = daemons.find((daemon) => daemon.id === node.daemon);
  const names = selectedDaemon
    ? selectedDaemon.providers
    : [...new Set(daemons.flatMap((daemon) => daemon.providers))].sort();
  return renderOptions(
    buildOptions({
      values: names,
      current: node.provider,
      noneLabel: tr('sop.field.none'),
      unmatchedLabel: (name) => tr('sop.providerCurrent', { name }),
    }),
    esc,
  );
}

/** One-line explanation of where the provider list came from. */
function pinHint(node: SopNode): string {
  const daemons = boot?.daemons ?? [];
  const selectedDaemon = daemons.find((daemon) => daemon.id === node.daemon);
  if (node.daemon !== '' && selectedDaemon) {
    return tr('sop.providerFromDaemon', { daemon: selectedDaemon.label });
  }
  return daemons.length > 0 ? tr('sop.providerFromAll') : '';
}

function mountCanvas(): void {
  const host = document.getElementById('agd-sop-canvas');
  if (!host) {
    canvas = null;
    return;
  }
  if (canvas) {
    view = canvas.getView();
  }
  const doc = model();
  // Read-only in inspect mode (no dragging, connecting, or renaming) and
  // *while* a run is in flight in edit mode: the canvas must show the spec
  // that was actually submitted. Once every node is terminal in edit mode
  // the graph is history, so editing resumes (and later marks the run stale).
  const editable =
    editorMode === 'edit' &&
    !runRunning() &&
    doc !== null &&
    boot?.parseError === null;
  const options = {
    editable,
    nodes: (doc?.nodes ?? []).map((node) => {
      const problems = nodeIssues(node);
      return {
        name: node.name,
        subtitle: node.identity || node.provider || '—',
        badge: node.output ? `→ ${node.output}` : '',
        selected: node.name === selected,
        status: statusOf(node.name),
        problem: problems.some((i) => i.level === 'error')
          ? ('error' as const)
          : problems.some((i) => i.level === 'warning')
            ? ('warning' as const)
            : problems.length > 0
              ? ('info' as const)
              : null,
      };
    }),
    edges: (doc?.nodes ?? []).flatMap((node) =>
      node.dependsOn.map((dep) => ({ from: dep, to: node.name })),
    ),
    positions,
    selected,
    view,
    onSelect: (name: string | null) => {
      selected = name;
      refreshSelectionUI();
      if (name && runActive()) {
        void loadNodeLog(name);
      }
    },
    onConnect: (from: string, to: string): boolean => {
      if (!doc || wouldCycle(doc, to, from)) {
        flash(tr('sop.cycleBlocked'), 'error');
        return false;
      }
      void apply({ kind: 'addDependency', node: to, dependsOn: from });
      return true;
    },
    onDeleteEdge: (from: string, to: string) => {
      void apply({ kind: 'removeDependency', node: to, dependsOn: from });
    },
    onMove: (name: string, point: SopPoint) => {
      persistLayout({ ...positions, [name]: point });
    },
    onRename: (name: string) => {
      selected = name;
      refreshSelectionUI();
      document.getElementById('agd-sop-node-name')?.focus();
    },
    onAddNode: () => {
      void apply({ kind: 'addNode', after: selected });
    },
  };
  // The webview re-renders its whole root on every edit, so the host element
  // is new each time: build a fresh canvas (carrying the saved view) instead
  // of updating a detached instance.
  canvas = new SopCanvas(host, options);
  canvas.render();
  if (!viewInitialized) {
    viewInitialized = true;
    canvas.fit();
  }
}

/* ── events ────────────────────────────────────────────────────────── */

function wireEvents(): void {
  document.querySelector('[data-mode="chat"]')?.addEventListener('click', () => void bridge.call('openChat'));
  document.querySelector('[data-mode="goal"]')?.addEventListener('click', () => void bridge.call('openGoal'));
  document.getElementById('agd-sop-lang')?.addEventListener('click', async () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    render();
    await bridge.call('setLanguage', { language: lang });
  });
  document.getElementById('agd-sop-settings')?.addEventListener('click', () => void bridge.call('openSettings'));
  document.querySelectorAll<HTMLElement>('[data-editor-mode]').forEach((element) => {
    element.addEventListener('click', () => {
      const next = element.dataset.editorMode as 'edit' | 'inspect';
      if (next === editorMode) {
        return;
      }
      editorMode = next;
      render();
      // Load the log for the already-selected node when entering inspect.
      if (next === 'inspect' && selected && runActive()) {
        void loadNodeLog(selected);
      } else if (next === 'inspect' && selected && boot?.run) {
        void loadNodeLog(selected);
      }
    });
  });
  document.getElementById('agd-sop-new')?.addEventListener('click', () => void call('sop.newSop'));
  document.querySelectorAll<HTMLElement>('[data-template]').forEach((element) => {
    element.addEventListener('click', () =>
      void call('sop.openTemplate', { path: element.dataset.template }),
    );
  });
  document.getElementById('agd-sop-add')?.addEventListener('click', () =>
    void apply({ kind: 'addNode', after: selected }),
  );
  document.getElementById('agd-sop-layout')?.addEventListener('click', () => {
    const doc = model();
    if (!doc) {
      return;
    }
    const next = computeLayout(doc, {});
    positions = next;
    persistLayout(next);
    canvas?.update({}, next);
    canvas?.fit();
  });
  document.getElementById('agd-sop-fit')?.addEventListener('click', () => canvas?.fit());
  document.getElementById('agd-sop-zoom-in')?.addEventListener('click', () => canvas?.zoom(0.1));
  document.getElementById('agd-sop-zoom-out')?.addEventListener('click', () => canvas?.zoom(-0.1));
  document.getElementById('agd-sop-preview')?.addEventListener('click', () => {
    showPreview = !showPreview;
    render();
  });
  document.getElementById('agd-sop-yaml')?.addEventListener('click', () => void call('sop.revealYaml'));
  document.getElementById('agd-sop-validate')?.addEventListener('click', () => void runValidate());
  document.getElementById('agd-sop-run')?.addEventListener('click', () => void startRun());
  document.getElementById('agd-sop-cancel-run')?.addEventListener('click', () => void cancelRun());
  document.getElementById('agd-sop-restart-server')?.addEventListener('click', async () => {
    busy = 'validate';
    render();
    let restarted = false;
    try {
      const res = await bridge.call<{ ok: boolean }>('sop.restartServer');
      restarted = res?.ok === true;
    } catch (err) {
      flash(String(err instanceof Error ? err.message : err), 'error');
    } finally {
      busy = null;
    }
    if (!restarted) {
      flash(tr('sop.restartFailed'), 'error');
      render();
      return;
    }
    await call('sop.boot');
    await runValidate();
  });
  document.getElementById('agd-sop-save')?.addEventListener('click', async () => {
    busy = 'save';
    render();
    try {
      await call('sop.save');
    } finally {
      busy = null;
      render();
    }
  });

  document.querySelectorAll<HTMLElement>('[data-problem-node]').forEach((element) => {
    element.addEventListener('click', () => {
      selected = element.dataset.problemNode ?? null;
      refreshSelectionUI();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-identity-delete]').forEach((element) => {
    element.addEventListener('click', () =>
      void apply({ kind: 'removeIdentity', alias: element.dataset.identityDelete ?? '' }),
    );
  });
  document.getElementById('agd-sop-identity-add')?.addEventListener('click', () => {
    newIdentityAlias = '';
    render();
    document.getElementById('agd-sop-identity-new')?.focus();
  });
  const newAlias = document.getElementById('agd-sop-identity-new') as HTMLInputElement | null;
  newAlias?.addEventListener('input', () => {
    newIdentityAlias = newAlias.value;
  });
  const commitAlias = (): void => {
    const alias = (newIdentityAlias ?? '').trim();
    newIdentityAlias = null;
    if (alias === '' || model()?.identities.some((i) => i.alias === alias)) {
      render();
      return;
    }
    void apply({ kind: 'addIdentity', alias });
  };
  newAlias?.addEventListener('change', commitAlias);
  newAlias?.addEventListener('keydown', (event) => {
    if ((event as KeyboardEvent).key === 'Enter') {
      commitAlias();
    }
  });
  document.querySelectorAll<HTMLInputElement>('[data-identity-field]').forEach((element) => {
    element.addEventListener('change', () => {
      const field = element.dataset.identityField as 'did' | 'provider';
      void apply({
        kind: 'setIdentityField',
        alias: element.dataset.alias ?? '',
        field,
        value: element.value,
      });
    });
  });
  document.getElementById('agd-sop-impact-cancel')?.addEventListener('click', () => {
    pendingImpact = null;
    render();
  });
  document.getElementById('agd-sop-impact-confirm')?.addEventListener('click', () => {
    const pending = pendingImpact;
    pendingImpact = null;
    if (pending) {
      void apply({ kind: 'deleteNode', name: pending.name });
    }
  });
}

/** Inspector bindings — re-run whenever only the inspector re-renders. */
function wireInspector(): void {
  document.querySelectorAll<HTMLElement>('[data-run]').forEach((element) => {
    element.addEventListener('click', () =>
      void selectRun(element.dataset.run ?? ''),
    );
  });
  const commit = (field: NodeField, value: string): void => {
    const node = selectedNode();
    if (!node) {
      return;
    }
    if (field === 'name') {
      if (value.trim() === '' || value === node.name) {
        return;
      }
      void apply({ kind: 'renameNode', from: node.name, to: value.trim() });
      return;
    }
    void apply({ kind: 'setNodeField', name: node.name, field, value });
  };
  const bindings: [string, NodeField][] = [
    ['agd-sop-node-name', 'name'],
    ['agd-sop-node-identity', 'identity'],
    ['agd-sop-node-input', 'input'],
    ['agd-sop-node-output', 'output'],
    ['agd-sop-node-provider', 'provider'],
    ['agd-sop-node-daemon', 'daemon'],
    ['agd-sop-node-dir', 'dir'],
  ];
  for (const [id, field] of bindings) {
    const element = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
    element?.addEventListener('change', () => commit(field, element.value));
  }
  document.getElementById('agd-sop-dep-add')?.addEventListener('change', (event) => {
    const value = (event.target as HTMLSelectElement).value;
    const node = selectedNode();
    if (!value || !node) {
      return;
    }
    const doc = model();
    if (doc && wouldCycle(doc, node.name, value)) {
      flash(tr('sop.cycleBlocked'), 'error');
      return;
    }
    void apply({ kind: 'addDependency', node: node.name, dependsOn: value });
  });
  document.querySelectorAll<HTMLElement>('[data-dep-remove]').forEach((element) => {
    element.addEventListener('click', () => {
      const node = selectedNode();
      if (node) {
        void apply({
          kind: 'removeDependency',
          node: node.name,
          dependsOn: element.dataset.depRemove ?? '',
        });
      }
    });
  });
  document.getElementById('agd-sop-delete-node')?.addEventListener('click', () => {
    const node = selectedNode();
    const doc = model();
    if (!node || !doc) {
      return;
    }
    const dependents = doc.nodes
      .filter((other) => other.dependsOn.includes(node.name))
      .map((other) => other.name);
    const placeholders = doc.nodes
      .filter((other) => other.input.includes(`\${${node.name}.`))
      .map((other) => other.name);
    pendingImpact = { kind: 'deleteNode', name: node.name, dependents, placeholders };
    render();
  });
  document.querySelectorAll<HTMLElement>('[data-artifact]').forEach((element) => {
    element.addEventListener('click', async () => {
      const uri = element.dataset.artifact ?? '';
      const node = selectedNode();
      try {
        await bridge.call('openArtifact', {
          artifactUri: uri,
          dir: node?.dir ?? null,
          workspaceFolder: boot?.workspaceFolder ?? null,
        });
      } catch (err) {
        flash(String(err instanceof Error ? err.message : err), 'error');
      }
    });
  });
  document.querySelectorAll<HTMLAnchorElement>('.agd-sop-log-card .agd-md a[href]').forEach((anchor) => {
    anchor.addEventListener('click', (event) => {
      event.preventDefault();
      void bridge.call('openExternal', { url: anchor.href });
    });
  });
}

async function main(): Promise<void> {
  applyBoot(await bridge.call<SopBoot>('sop.boot'));
  render();
  bridge.onEvent((event, payload) => {
    if (event === 'refresh') {
      applyBoot(payload as SopBoot);
      render();
    }
  });
}

void main();
