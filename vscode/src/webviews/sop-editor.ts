import './mode-switcher.css';
import './markdown.css';
import './sop-editor.css';
import { renderMarkdown } from './markdown';
import { t, type MessageKey } from '../core/i18n';
import { issues as localIssues, loopMembership, wouldCycle } from '../core/sop/graph';
import { computeLayout, type SopPoint } from '../core/sop/layout';
import { buildOptions, renderOptions } from '../core/sop/options';
import type { EditIntent, SopDoc, SopIssue, SopNode } from '../core/sop/model';
import type {
  ServerState,
  SopNodeLog,
  SopNodeStatus,
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
  sidebarWidth: number | null;
  inspectorWidth: number | null;
  trackedRun: SopRunSummary | null;
  runs: SopRunSummary[];
  run: SopRunSummary | null;
}

interface SopRunSummary {
  rootJobId: string;
  name?: string;
  status?: string;
  provider?: string;
  daemonId?: string | null;
  updatedAt?: string;
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

const OUTPUT_EXTENSIONS = ['.md', '.txt', '.json', '.yaml', '.yml', '.html', '.csv', '.py', '.ts', '.js'] as const;

let boot: SopBoot | null = null;
let lang: UiLanguage = 'zh';
let selected: string | null = null;
let selectedInstanceId: string | null = null;
let loopSettingsName: string | null = null;
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
const DEFAULT_SIDEBAR_WIDTH = 248;
const MIN_SIDEBAR_WIDTH = 200;
const MAX_SIDEBAR_WIDTH = 520;

function runActive(): boolean {
  return boot?.trackedRun !== null && boot?.trackedRun !== undefined;
}

function inspectRunRoot(): string | null {
  return boot?.run?.rootJobId ?? null;
}

function runRunning(): boolean {
  return runActive() && !(runStatus?.all_terminal ?? false);
}

function statusInstanceId(node: SopNodeStatus): string {
  return node.loop_name && node.iteration > 0
    ? `${node.name}#${node.iteration}`
    : node.name;
}

function baseNodeName(instanceId: string): string {
  const hash = instanceId.lastIndexOf('#');
  return hash < 0 ? instanceId : instanceId.slice(0, hash);
}

function statusNodes(): SopNodeStatus[] {
  return runStatus?.nodes ?? [];
}

function selectedStatusNodes(): SopNodeStatus[] {
  return selectedRunStatus?.nodes ?? [];
}

function statusById(nodes: SopNodeStatus[], id: string): SopNodeStatus | undefined {
  return nodes.find((node) => statusInstanceId(node) === id);
}

function latestStatusOf(name: string, nodes: SopNodeStatus[]): SopNodeStatus | undefined {
  const matches = nodes.filter((node) => node.name === name);
  return matches.length > 0 ? matches[matches.length - 1] : undefined;
}

function statusOf(name: string): string {
  const nodes = statusNodes();
  if (selectedInstanceId) {
    return statusById(nodes, selectedInstanceId)?.status ?? '';
  }
  return latestStatusOf(name, nodes)?.status ?? '';
}

function selectedStatusOf(name: string): string {
  const nodes = selectedStatusNodes();
  if (selectedInstanceId) {
    return statusById(nodes, selectedInstanceId)?.status ?? '';
  }
  return latestStatusOf(name, nodes)?.status ?? '';
}

function formatRunTime(startedAt: number): string {
  return new Date(startedAt).toLocaleString();
}

function formatRunDate(updatedAt: string | undefined, startedAt: number): string {
  if (!updatedAt) return formatRunTime(startedAt);
  const ms = Date.parse(updatedAt);
  return Number.isNaN(ms) ? formatRunTime(startedAt) : formatRunTime(ms);
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

function outputExtension(value: string): string {
  const match = value.match(/\.[^./\\]+$/);
  return match?.[0] ?? '';
}

function isKnownOutputExtension(value: string): boolean {
  return (OUTPUT_EXTENSIONS as readonly string[]).includes(value);
}

const INPUT_REFERENCE_RE = /\$\{([A-Za-z0-9_.-]+)\.([^}]+)\}/g;

function referenceTokenHtml(node: string, output: string): string {
  const raw = `\${${node}.${output}}`;
  return `<span class="agd-sop-ref-token" contenteditable="false" data-ref="${esc(raw)}"><span class="agd-sop-ref-node">${esc(node)}</span><span class="agd-sop-ref-arrow">→</span><span class="agd-sop-ref-output">${esc(output)}</span></span>`;
}

function richInputHtml(raw: string): string {
  let html = '';
  let last = 0;
  for (const match of raw.matchAll(INPUT_REFERENCE_RE)) {
    const index = match.index ?? 0;
    html += esc(raw.slice(last, index)).replaceAll('\n', '<br>');
    html += referenceTokenHtml(match[1], match[2]);
    last = index + match[0].length;
  }
  html += esc(raw.slice(last)).replaceAll('\n', '<br>');
  return html;
}

function serializeRichInput(root: Node): string {
  let out = '';
  for (const child of root.childNodes) {
    if (child.nodeType === Node.TEXT_NODE) {
      out += child.textContent ?? '';
      continue;
    }
    if (!(child instanceof HTMLElement)) {
      continue;
    }
    if (child.dataset.ref) {
      out += child.dataset.ref;
      continue;
    }
    if (child.tagName === 'BR') {
      out += '\n';
      continue;
    }
    const block = child.tagName === 'DIV' || child.tagName === 'P';
    if (block && out !== '' && !out.endsWith('\n')) {
      out += '\n';
    }
    out += serializeRichInput(child);
  }
  return out;
}

function inputCaretContext(editor: HTMLElement): {
  query: string;
  slashNode: Text | null;
  slashOffset: number;
} {
  const selection = window.getSelection();
  if (!selection?.rangeCount || !selection.isCollapsed) {
    return { query: '', slashNode: null, slashOffset: -1 };
  }
  const caret = selection.getRangeAt(0);
  if (!editor.contains(caret.startContainer)) {
    return { query: '', slashNode: null, slashOffset: -1 };
  }

  type Character = { char: string; node: Text | null; offset: number };
  const chars: Character[] = [];
  let stopped = false;
  const walk = (node: Node): void => {
    if (stopped) return;
    if (node.nodeType === Node.TEXT_NODE) {
      const text = node as Text;
      const end = text === caret.startContainer ? caret.startOffset : text.data.length;
      for (let i = 0; i < end; i += 1) {
        chars.push({ char: text.data[i], node: text, offset: i });
      }
      if (text === caret.startContainer) stopped = true;
      return;
    }
    if (node instanceof HTMLElement && node.dataset.ref) {
      chars.push({ char: ' ', node: null, offset: -1 });
      return;
    }
    if (node instanceof HTMLBRElement) {
      chars.push({ char: '\n', node: null, offset: -1 });
      return;
    }
    for (const child of node.childNodes) walk(child);
  };
  walk(editor);

  let slash: Character | null = null;
  let query = '';
  for (const item of chars) {
    if (/\s/.test(item.char)) {
      slash = null;
      query = '';
    } else if (item.char === '/') {
      slash = item;
      query = '';
    } else if (slash) {
      query += item.char;
    }
  }
  return {
    query,
    slashNode: slash?.node ?? null,
    slashOffset: slash?.offset ?? -1,
  };
}

/**
 * The provider the canvas should brand: explicit task provider wins, then a
 * static identity provider. DID-only identities resolve their provider from
 * server bindings at submit time, so the canvas uses the alias fallback there.
 */
function canvasProvider(node: SopNode): string {
  const identity = model()?.identities.find((item) => item.alias === node.identity);
  return node.provider || identity?.provider || node.identity || '';
}

function nodeIssues(node: SopNode): SopIssue[] {
  return allIssues().filter((issue) => issue.node === node.name);
}

function nodeProblems(node: SopNode): 'error' | 'warning' | 'info' | null {
  const problems = nodeIssues(node);
  if (problems.some((issue) => issue.level === 'error')) {
    return 'error';
  }
  if (problems.some((issue) => issue.level === 'warning')) {
    return 'warning';
  }
  return problems.length > 0 ? 'info' : null;
}

function templateByName(doc: SopDoc | null, name: string): SopNode {
  return (
    doc?.nodes.find((node) => node.name === name) ?? {
      name,
      identity: '',
      dependsOn: [],
      input: '',
      output: '',
      provider: '',
      daemon: '',
      dir: '',
      index: 0,
    }
  );
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
    selectedInstanceId = null;
  }
  if (
    loopSettingsName &&
    !next.model?.loops.some((loop) => loop.name === loopSettingsName)
  ) {
    loopSettingsName = null;
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

function clampSidebarWidth(width: number, containerWidth: number): number {
  const max = Math.max(
    MIN_SIDEBAR_WIDTH,
    Math.min(MAX_SIDEBAR_WIDTH, containerWidth - 240 - 288 - 12),
  );
  return Math.max(MIN_SIDEBAR_WIDTH, Math.min(max, width));
}

function applySidebarWidth(width: number | null): void {
  const root = document.querySelector<HTMLElement>('.agd-sop');
  if (root && width !== null) {
    root.style.setProperty('--agd-sop-sidebar-width', `${Math.round(width)}px`);
  }
}

function wireSidebarResizer(): void {
  const handle = document.getElementById('agd-sop-sidebar-resizer');
  const container = document.querySelector<HTMLElement>('.agd-sop-body');
  if (!handle || !container) return;

  handle.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    container.classList.add('is-resizing');
    handle.setPointerCapture(event.pointerId);

    const move = (moveEvent: PointerEvent): void => {
      const rect = container.getBoundingClientRect();
      const width = clampSidebarWidth(
        moveEvent.clientX - rect.left,
        rect.width,
      );
      if (boot) boot.sidebarWidth = width;
      applySidebarWidth(width);
    };

    const finish = (): void => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', finish);
      window.removeEventListener('pointercancel', finish);
      container.classList.remove('is-resizing');
      if (boot?.sidebarWidth !== null && boot?.sidebarWidth !== undefined) {
        void bridge.call('sop.setSidebarWidth', { width: boot.sidebarWidth });
      }
    };

    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', finish);
    window.addEventListener('pointercancel', finish);
  });
}

function clampInspectorWidth(width: number, containerWidth: number): number {
  const sidebar = boot?.sidebarWidth ?? DEFAULT_SIDEBAR_WIDTH;
  const max = Math.max(280, containerWidth - sidebar - 320 - 12);
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
  const statuses = statusNodes();
  const renderedIds = [...document.querySelectorAll<HTMLElement>('.agd-sop-node')].map(
    (element) => element.dataset.node ?? '',
  );
  const expectedIds = statuses.map((node) => statusInstanceId(node));
  const sameGraph =
    renderedIds.length === expectedIds.length &&
    renderedIds.every((id) => expectedIds.includes(id));
  if (!sameGraph) {
    render();
    return;
  }
  for (const element of document.querySelectorAll<HTMLElement>('.agd-sop-node')) {
    const name = element.dataset.node ?? '';
    const status = statusById(statuses, name)?.status ?? '';
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

async function ensureSelectedRunStatus(): Promise<void> {
  const rootJobId = inspectRunRoot();
  if (!rootJobId || selectedRunStatusRoot === rootJobId) {
    return;
  }
  try {
    selectedRunStatus = await bridge.call<SopStatus>('sop.status', { rootJobId });
    selectedRunStatusRoot = rootJobId;
  } catch {
    selectedRunStatus = null;
    selectedRunStatusRoot = rootJobId;
  }
  render();
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
  } else {
    await ensureSelectedRunStatus();
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
    element.classList.toggle(
      'is-selected',
      element.dataset.node === (selectedInstanceId ?? selected),
    );
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
    const problems = localIssues(model() ?? { name: '', identities: [], nodes: [], loops: [] });
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
  applySidebarWidth(boot.sidebarWidth);
  const doc = model();
  if (!doc) {
    // No template bound yet: guide instead of showing dead chrome. The
    // toolbar stays rendered but disabled (see renderToolbar).
    root.innerHTML = `
    <div class="agd-sop is-empty" style="--agd-sop-sidebar-width:${boot.sidebarWidth ? `${Math.round(boot.sidebarWidth)}px` : `${DEFAULT_SIDEBAR_WIDTH}px`}">
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
        <div class="agd-sop-splitter agd-sop-sidebar-resizer" id="agd-sop-sidebar-resizer" role="separator" aria-orientation="vertical" title="${tr('sop.resizeSidebar')}"></div>
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
    wireSidebarResizer();
    return;
  }
  root.innerHTML = `
    <div class="agd-sop ${editorMode === 'inspect' ? 'is-inspect' : ''}" style="--agd-sop-sidebar-width:${boot.sidebarWidth ? `${Math.round(boot.sidebarWidth)}px` : `${DEFAULT_SIDEBAR_WIDTH}px`};--agd-sop-inspector-width:${boot.inspectorWidth ? `${Math.round(boot.inspectorWidth)}px` : '288px'}">
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
      <div class="agd-sop-splitter agd-sop-sidebar-resizer" id="agd-sop-sidebar-resizer" role="separator" aria-orientation="vertical" title="${tr('sop.resizeSidebar')}"></div>
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
    ${loopSettingsName ? renderLoopSettings() : ''}
  </div>`;

  mountCanvas();
  wireEvents();
  wireInspector();
  wireSidebarResizer();
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
          <span class="agd-sop-run-title">
            <span class="agd-mono">${esc(run.rootJobId.slice(0, 8))}</span>
            <strong>${esc(run.name ?? run.rootJobId)}</strong>
          </span>
          <span class="agd-sop-run-meta">
            ${run.status ? `<small class="agd-sop-run-status is-${esc(run.status)}">${esc(run.status)}</small>` : ''}
            ${run.provider ? `<small>${esc(run.provider)}</small>` : ''}
            <small>${esc(formatRunDate(run.updatedAt, run.startedAt))}</small>
          </span>
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

function renderLoopSettings(): string {
  const loop = model()?.loops.find((item) => item.name === loopSettingsName);
  if (!loop) {
    return '';
  }
  return `
  <div class="agd-sop-modal" id="agd-sop-loop-settings-backdrop">
    <div class="agd-sop-modal-body">
      <h3>${tr('sop.loopSettings')}</h3>
      <p>${esc(loop.name)} · ${tr('sop.loopEntry')}: ${esc(loop.entry || loop.nodes[0] || '—')}</p>
      <label class="agd-sop-field">
        <span>${tr('sop.loopMaxIterations')}</span>
        <input id="agd-sop-loop-iterations" class="agd-input" type="number" min="1" max="100" value="${loop.maxIterations || 2}">
      </label>
      <div class="agd-sop-field">
        <span>${tr('sop.loopMembers')}</span>
        <div class="agd-sop-deps">${loop.nodes.map((name) => `<span class="agd-sop-dep">${esc(name)}</span>`).join('')}</div>
      </div>
      <div class="agd-actions">
        <button type="button" id="agd-sop-loop-cancel" class="agd-btn">${tr('sop.impact.cancel')}</button>
        <button type="button" id="agd-sop-loop-save" class="agd-btn agd-btn-primary">${tr('sop.loopSave')}</button>
      </div>
    </div>
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
    <div class="agd-sop-field">
      <span>${tr('sop.field.input')}</span>
      <div class="agd-sop-input-completion">
        <div class="agd-input agd-sop-rich-input" id="agd-sop-node-input" contenteditable="true" role="textbox" aria-multiline="true" data-placeholder="${tr('sop.field.inputPh')}">${richInputHtml(node.input)}</div>
        <div class="agd-sop-completion" id="agd-sop-input-suggestions" hidden></div>
      </div>
      <small class="agd-sop-input-hint">${tr('sop.inputSlashHint')}</small>
    </div>
    <div class="agd-sop-field">
      <span>${tr('sop.field.output')}</span>
      <div class="agd-sop-output-row">
        <input class="agd-input agd-mono" id="agd-sop-node-output" value="${esc(node.output)}">
        <select class="agd-input agd-mono" id="agd-sop-output-ext" title="${tr('sop.outputExtension')}">
          <option value="">${tr('sop.outputExtNone')}</option>
          ${OUTPUT_EXTENSIONS.map((ext) => `<option value="${esc(ext)}" ${outputExtension(node.output) === ext ? 'selected' : ''}>${esc(ext)}</option>`).join('')}
          <option value="__other__" ${outputExtension(node.output) && !isKnownOutputExtension(outputExtension(node.output)) ? 'selected' : ''}>${tr('sop.outputExtOther')}</option>
        </select>
      </div>
      <input class="agd-input agd-mono agd-sop-output-custom" id="agd-sop-output-ext-custom" placeholder="${tr('sop.outputExtCustomPh')}" value="${outputExtension(node.output) && !isKnownOutputExtension(outputExtension(node.output)) ? esc(outputExtension(node.output)) : ''}" ${outputExtension(node.output) && !isKnownOutputExtension(outputExtension(node.output)) ? '' : 'hidden'}>
    </div>
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
          <span class="agd-mono">${esc(step.task_id.slice(0, 8))}${step.iteration > 0 ? ` · ${tr('sop.loopIteration', { iteration: step.iteration })}` : ''}</span>
          <span class="agd-dim">${esc(step.status)}${step.exit_code === null ? '' : ` · exit ${step.exit_code}`}</span>
        </div>
        ${step.stdout ? `<div class="agd-sop-stdout agd-md">${renderMarkdown(step.stdout)}</div>` : ''}
        ${step.stderr ? `<div class="agd-sop-stderr agd-md">${renderMarkdown(step.stderr)}</div>` : ''}
      </div>`,
    )
    .join('');
  const selectedStatus = selectedInstanceId
    ? statusById(selectedStatusNodes(), selectedInstanceId)
    : latestStatusOf(node.name, selectedStatusNodes());
  const artifacts = selectedStatus?.artifact_details ?? [];
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

function activeStatusGraph(): SopStatus | null {
  if (runActive() && runStatus && !runStatus.all_terminal) {
    return runStatus;
  }
  if (editorMode === 'inspect' && selectedRunStatus) {
    return selectedRunStatus;
  }
  return null;
}

/**
 * The runtime graph expands every loop iteration into its own node
 * (``review#1``, ``review#2``) using the backend-provided `depends_on`
 * display names, so monitor nodes never overwrite each other.
 */
function runtimeGraph(doc: SopDoc, status: SopStatus | null): {
  nodes: SopNodeStatus[];
  edges: { from: string; to: string; kind?: 'loop' | 'iteration' }[];
  positions: Record<string, SopPoint>;
} | null {
  if (!status) {
    return null;
  }
  const statuses = [...status.nodes].sort(
    (a, b) => (a.iteration - b.iteration) || a.name.localeCompare(b.name),
  );
  const byId = new Map<string, SopNodeStatus>();
  const ids: string[] = [];
  for (const node of statuses) {
    const id = statusInstanceId(node);
    if (byId.has(id)) {
      continue;
    }
    byId.set(id, node);
    ids.push(id);
  }

  const edges: { from: string; to: string; kind?: 'loop' | 'iteration' }[] = [];
  for (const node of statuses) {
    const to = statusInstanceId(node);
    for (const dep of node.depends_on) {
      if (!byId.has(dep)) {
        continue;
      }
      const parent = byId.get(dep);
      if (parent && parent.loop_name && parent.loop_name === node.loop_name) {
        edges.push({
          from: dep,
          to,
          kind: parent.iteration === node.iteration ? 'loop' : 'iteration',
        });
      } else {
        edges.push({ from: dep, to });
      }
    }
  }

  const templateByName = new Map(
    doc.nodes.map((node) => [node.name, node]),
  );
  const pseudo: SopDoc = {
    name: doc.name,
    identities: doc.identities,
    loops: [],
    nodes: ids.map((id) => ({
      ...(templateByName.get(baseNodeName(id)) ?? {}),
      name: id,
      identity: '',
      dependsOn: byId.get(id)?.depends_on ?? [],
      input: '',
      output: '',
      provider: '',
      daemon: '',
      dir: '',
      index: 0,
    })),
  };
  const positions = computeLayout(pseudo);
  return { nodes: statuses, edges, positions };
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
  const runtime = doc ? runtimeGraph(doc, activeStatusGraph()) : null;
  const membership = doc ? loopMembership(doc) : new Map<string, string>();
  const loopsByName = new Map((doc?.loops ?? []).map((loop) => [loop.name, loop]));
  const displayPositions = runtime ? runtime.positions : positions;
  const templateNodes = (doc?.nodes ?? []).map((node) => ({
    name: node.name,
    provider: canvasProvider(node),
    badge: node.output ? `→ ${node.output}` : '',
    loopName: membership.get(node.name),
    loopIterations: loopsByName.get(membership.get(node.name) ?? '')?.maxIterations,
    loopEntry: loopsByName.get(membership.get(node.name) ?? '')?.entry === node.name,
    selected: node.name === (selectedInstanceId ?? selected),
    status: statusOf(node.name),
    problem: nodeProblems(node),
  }));
  const runtimeNodes = runtime
    ? runtime.nodes.map((node) => ({
        name: statusInstanceId(node),
        provider: node.provider || canvasProvider(templateByName(doc, node.name)),
        badge: templateByName(doc, node.name).output
          ? `→ ${templateByName(doc, node.name).output}`
          : '',
        loopName: node.loop_name ?? undefined,
        selected: statusInstanceId(node) === (selectedInstanceId ?? selected),
        status: node.status,
        problem: null,
      }))
    : [];
  const nodes = runtime ? runtimeNodes : templateNodes;
  const edges = runtime
    ? runtime.edges
    : (doc?.nodes ?? []).flatMap((node) =>
        node.dependsOn.map((dep) => ({
          from: dep,
          to: node.name,
          kind:
            membership.get(dep) !== undefined &&
            membership.get(dep) === membership.get(node.name)
              ? ('loop' as const)
              : undefined,
        })),
      );
  const options = {
    editable,
    nodes,
    edges,
    positions: displayPositions,
    selected: selectedInstanceId ?? selected,
    view,
    onLoopSettings: (name: string) => {
      loopSettingsName = name;
      render();
    },
    onSelect: (name: string | null) => {
      if (name) {
        const base = baseNodeName(name);
        selected = doc?.nodes.some((node) => node.name === base) ? base : name;
        selectedInstanceId = runtime ? name : null;
      } else {
        selected = null;
        selectedInstanceId = null;
      }
      refreshSelectionUI();
      if (name && activeStatusGraph()) {
        void loadNodeLog(selected ?? name);
      }
    },
    onConnect: (from: string, to: string): boolean => {
      if (!doc) {
        return false;
      }
      const createsLoop = wouldCycle(doc, to, from);
      void apply({ kind: 'addDependency', node: to, dependsOn: from }).then((applied) => {
        if (applied && createsLoop) {
          flash(tr('sop.loopAutoCreated'));
        }
      });
      return true;
    },
    onDeleteEdge: (from: string, to: string) => {
      void apply({ kind: 'removeDependency', node: to, dependsOn: from });
    },
    onMove: (name: string, point: SopPoint) => {
      if (!runtime) {
        persistLayout({ ...positions, [name]: point });
      }
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
      selectedInstanceId = null;
      render();
      if (next === 'inspect' && boot?.run) {
        void ensureSelectedRunStatus();
      }
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
      selectedInstanceId = null;
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
  document.getElementById('agd-sop-loop-settings-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget) {
      loopSettingsName = null;
      render();
    }
  });
  document.getElementById('agd-sop-loop-cancel')?.addEventListener('click', () => {
    loopSettingsName = null;
    render();
  });
  document.getElementById('agd-sop-loop-save')?.addEventListener('click', () => {
    const input = document.getElementById('agd-sop-loop-iterations') as HTMLInputElement | null;
    const name = loopSettingsName;
    const value = Math.max(1, Math.min(100, Number(input?.value) || 2));
    loopSettingsName = null;
    if (name) {
      void apply({ kind: 'setLoopField', name, field: 'maxIterations', value });
    }
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
    ['agd-sop-node-output', 'output'],
    ['agd-sop-node-provider', 'provider'],
    ['agd-sop-node-daemon', 'daemon'],
    ['agd-sop-node-dir', 'dir'],
  ];
  for (const [id, field] of bindings) {
    const element = document.getElementById(id) as HTMLInputElement | HTMLTextAreaElement | null;
    element?.addEventListener('change', () => commit(field, element.value));
  }

  const input = document.getElementById('agd-sop-node-input') as HTMLElement | null;
  const suggestions = document.getElementById('agd-sop-input-suggestions');
  const updateSuggestions = (): void => {
    const node = selectedNode();
    const doc = model();
    if (!input || !suggestions || !node || !doc) {
      if (suggestions) suggestions.hidden = true;
      return;
    }
    const { query, slashNode } = inputCaretContext(input);
    if (!slashNode || query.length >= 40) {
      suggestions.hidden = true;
      suggestions.innerHTML = '';
      return;
    }
    const needle = query.toLowerCase();
    const dependencies = node.dependsOn
      .map((name) => doc.nodes.find((other) => other.name === name))
      .filter((other): other is NonNullable<typeof other> => Boolean(other?.output))
      .filter((other) =>
        other.output.toLowerCase().includes(needle) ||
        other.name.toLowerCase().includes(needle),
      );
    suggestions.hidden = dependencies.length === 0;
    suggestions.innerHTML = dependencies.length === 0
      ? `<span class="agd-sop-completion-empty">${tr('sop.inputNoSuggestions')}</span>`
      : dependencies
          .map((other) => `
            <button type="button" class="agd-sop-completion-item" data-suggestion-node="${esc(other.name)}" data-suggestion-output="${esc(other.output)}">
              <strong>${esc(other.output)}</strong>
              <small>${esc(other.name)}</small>
            </button>`,
          )
          .join('');
  };
  input?.addEventListener('input', updateSuggestions);
  input?.addEventListener('click', updateSuggestions);
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && suggestions) {
      suggestions.hidden = true;
    }
  });
  input?.addEventListener('blur', () => {
    window.setTimeout(() => {
      if (suggestions && !suggestions.contains(document.activeElement)) {
        suggestions.hidden = true;
      }
    }, 80);
    if (input) commit('input', serializeRichInput(input));
  });
  suggestions?.addEventListener('mousedown', (event) => {
    const button = (event.target as HTMLElement | null)?.closest<HTMLElement>(
      '[data-suggestion-output]',
    );
    if (!button || !input) return;
    event.preventDefault();
    const context = inputCaretContext(input);
    if (!context.slashNode) return;
    const selection = window.getSelection();
    if (!selection?.rangeCount) return;
    const range = selection.getRangeAt(0);
    const endContainer = range.endContainer;
    const endOffset = range.endOffset;
    range.setStart(context.slashNode, context.slashOffset);
    range.setEnd(endContainer, endOffset);
    range.deleteContents();
    const holder = document.createElement('span');
    holder.innerHTML = referenceTokenHtml(
      button.dataset.suggestionNode ?? '',
      button.dataset.suggestionOutput ?? '',
    );
    const token = holder.firstElementChild;
    if (!token) return;
    range.insertNode(token);
    const space = document.createTextNode(' ');
    token.after(space);
    range.setStartAfter(space);
    range.collapse(true);
    selection.removeAllRanges();
    selection.addRange(range);
    suggestions.hidden = true;
    commit('input', serializeRichInput(input));
  });

  const output = document.getElementById('agd-sop-node-output') as HTMLInputElement | null;
  const outputExtensionSelect = document.getElementById('agd-sop-output-ext') as HTMLSelectElement | null;
  const outputExtensionInput = document.getElementById('agd-sop-output-ext-custom') as HTMLInputElement | null;
  outputExtensionSelect?.addEventListener('change', () => {
    const node = selectedNode();
    if (!node || !output) return;
    const base = node.output.replace(/\.[^./\\]+$/, '');
    if (outputExtensionSelect.value === '__other__') {
      if (outputExtensionInput) {
        outputExtensionInput.hidden = false;
        outputExtensionInput.value = '';
        outputExtensionInput.focus();
      }
      return;
    }
    output.value = `${base}${outputExtensionSelect.value}`;
    if (outputExtensionInput) outputExtensionInput.hidden = true;
    commit('output', output.value);
  });
  outputExtensionInput?.addEventListener('change', () => {
    const node = selectedNode();
    if (!node || !output) return;
    const raw = outputExtensionInput.value.trim();
    const suffix = raw === '' ? '' : raw.startsWith('.') ? raw : `.${raw}`;
    const base = node.output.replace(/\.[^./\\]+$/, '');
    output.value = `${base}${suffix}`;
    commit('output', output.value);
  });
  document.getElementById('agd-sop-dep-add')?.addEventListener('change', (event) => {
    const value = (event.target as HTMLSelectElement).value;
    const node = selectedNode();
    if (!value || !node) {
      return;
    }
    const doc = model();
    const createsLoop = doc ? wouldCycle(doc, node.name, value) : false;
    void apply({ kind: 'addDependency', node: node.name, dependsOn: value }).then((applied) => {
      if (applied && createsLoop) {
        flash(tr('sop.loopAutoCreated'));
      }
    });
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
