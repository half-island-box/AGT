import './mode-switcher.css';
import './markdown.css';
import './chat.css';
import { renderMarkdown } from './markdown';
import { isLocalDaemon } from '../core/daemons';
import { providerLogo } from '../core/icons';
import { t, type MessageKey } from '../core/i18n';
import type {
  AgentEvent,
  AgentUsage,
  ChatInputMessage,
  ChatSessionInfo,
  DaemonInfo,
  IdentityWithBindings,
  InputUploadInfo,
  MessagePart,
  RuntimeInfo,
  SessionHistoryInfo,
  UiLanguage,
} from '../core/types';
import { bridge } from './bridge';
import { openDirPicker } from './dir-picker';

interface ChatBoot {
  language: UiLanguage;
  state: 'unknown' | 'offline' | 'starting' | 'online';
  daemons: DaemonInfo[];
  runtimes: RuntimeInfo[];
  identities: IdentityWithBindings[];
  sessions: ChatSessionInfo[];
  workspaceFolders: string[];
  workspaceRoot: string;
  selection?: ChatSelection;
}

interface ChatSelection {
  daemonId: string;
  provider?: string;
}

interface ChatMessageFile {
  name: string;
  size: number;
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  provider?: string;
  streaming?: boolean;
  /** Attachments this turn carried (what the user sent, not the uploads). */
  files?: ChatMessageFile[];
  /** Structured transcript (thinking / tool calls) for this assistant turn. */
  events?: AgentEvent[];
  /** Turn-level usage summary (from ``done`` or the snapshot). */
  usage?: AgentUsage | null;
}

/**
 * One renderable piece of an assistant turn.
 *
 * Text arrives on the plain ``stdout`` channel; the events are spliced back
 * in by ``text_offset`` at render time, so the stream never duplicates text
 * across two channels.
 */
type ChatBlock =
  | { type: 'text'; text: string }
  | { type: 'thinking'; key: string; text: string; done: boolean }
  | {
      type: 'tool_use';
      key: string;
      toolUseId: string;
      name: string;
      input?: unknown;
      done: boolean;
    }
  | { type: 'tool_result'; toolUseId: string; content: string; isError: boolean };

/**
 * A file the user picked but has not sent yet. ``uploading`` covers the
 * eager ``POST /api/uploads`` that happens as soon as the file is selected,
 * so sending never blocks on a big file.
 */
interface ChatAttachment extends ChatMessageFile {
  id: number;
  mimeType: string;
  status: 'uploading' | 'ready' | 'error';
  uploadId?: string;
  uri?: string;
  error?: string;
}

interface ActiveJob {
  jobId: string;
  taskId: string;
  status: string;
  sessionId?: string | null;
}

interface SelectSessionResult {
  resolved: {
    job_id: string;
    task_id: string;
    session_id: string;
    provider: string;
    identity_did: string | null;
    daemon_id: string | null;
    dir: string | null;
  };
  jobStatus: { status: string };
  history?: SessionHistoryInfo;
  historyError?: string;
}

interface DeleteSessionResult {
  deleted: { ok: boolean };
  sessions: ChatSessionInfo[];
}

type ChatEvent = Record<string, unknown> & { kind?: string };
type MockSendResult = { events?: ChatEvent[] };
type BusyState = 'idle' | 'sending' | 'settling' | 'cancelling';

// Match the Server's defaults (ServerConfig.max_upload_bytes / the 10-file
// per-turn cap); the Server still enforces both, this is early feedback.
const MAX_ATTACHMENT_BYTES = 32 * 1024 * 1024;
const MAX_ATTACHMENTS = 10;

const SIDEBAR_WIDTH_KEY = 'agt.chat.sidebarWidth';
const SIDEBAR_COLLAPSED_KEY = 'agt.chat.sidebarCollapsed';
const DEFAULT_SIDEBAR_WIDTH = 292;
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 520;

let boot: ChatBoot | undefined;
let lang: UiLanguage = 'zh';
let selectedDaemonId: string | null = null;
let selectedProvider: string | null = null;
let selectedIdentityDid = '';
let agentCwd = '';
let agentCwdTouched = false;
let sessions: ChatSessionInfo[] = [];
let sessionsLoading = false;
/** Guards against a slow list overwriting a newer one. */
let sessionsRequestSeq = 0;
let pendingDeleteSession: ChatSessionInfo | null = null;
let pendingDeleteDaemonId: string | null = null;
let deletingSession = false;
let activeSessionId: string | null = null;
let currentJob: ActiveJob | null = null;
let messages: ChatMessage[] = [];
let busy: BusyState = 'idle';
let notice: string | null = null;
let openDropdown: 'daemon' | 'agent' | 'identity' | null = null;
let messageSeq = 0;
let loadingBoot = true;
let documentClickWired = false;
let promptHistory: string[] = [];
let promptHistoryIndex: number | null = null;
let promptDraft = '';
let attachments: ChatAttachment[] = [];
let attachmentSeq = 0;

function storedSidebarWidth(): number {
  const value = Number(window.localStorage.getItem(SIDEBAR_WIDTH_KEY));
  return Number.isFinite(value)
    ? Math.min(MAX_SIDEBAR_WIDTH, Math.max(MIN_SIDEBAR_WIDTH, Math.round(value)))
    : DEFAULT_SIDEBAR_WIDTH;
}

let sidebarWidth = storedSidebarWidth();
let sidebarCollapsed = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY) === '1';

function recallPrompt(event: KeyboardEvent, input: HTMLTextAreaElement): void {
  if (promptHistory.length === 0) return;
  const value = input.value;
  const selectionStart = input.selectionStart ?? 0;
  const selectionEnd = input.selectionEnd ?? 0;
  const onFirstLine = !value.slice(0, selectionStart).includes('\n');
  const onLastLine = !value.slice(selectionEnd).includes('\n');
  const caretFree = selectionStart === selectionEnd;

  if (event.key === 'ArrowUp' && caretFree && onFirstLine) {
    event.preventDefault();
    if (promptHistoryIndex === null) {
      promptDraft = value;
      promptHistoryIndex = promptHistory.length - 1;
    } else {
      promptHistoryIndex = Math.max(0, promptHistoryIndex - 1);
    }
  } else if (event.key === 'ArrowDown' && promptHistoryIndex !== null && caretFree && onLastLine) {
    event.preventDefault();
    promptHistoryIndex += 1;
    if (promptHistoryIndex >= promptHistory.length) {
      promptHistoryIndex = null;
      input.value = promptDraft;
      input.setSelectionRange(input.value.length, input.value.length);
      return;
    }
  } else {
    return;
  }

  if (promptHistoryIndex !== null) {
    input.value = promptHistory[promptHistoryIndex];
    input.setSelectionRange(input.value.length, input.value.length);
  }
}

function esc(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function tr(key: MessageKey, vars?: Record<string, string | number>): string {
  return t(lang, key, vars);
}

function copyIcon(): string {
  return `<svg class="agd-copy-icon" viewBox="0 0 16 16" aria-hidden="true">
    <rect x="5.75" y="5.75" width="7.5" height="7.5" rx="1.5"></rect>
    <path d="M10.25 3.25H4.75a1.5 1.5 0 0 0-1.5 1.5v5.5"></path>
  </svg>`;
}

function checkIcon(): string {
  return `<svg class="agd-copy-icon" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M3 8.5 6.5 12 13 4.5"></path>
  </svg>`;
}

function trashIcon(): string {
  return `<svg viewBox="0 0 16 16" aria-hidden="true">
    <path d="M2.75 4.75h10.5"></path>
    <path d="M6.25 4.75V3.4a.65.65 0 0 1 .65-.65h2.2a.65.65 0 0 1 .65.65v1.35"></path>
    <path d="M4.4 4.75l.45 7.5a.9.9 0 0 0 .9.85h4.5a.9.9 0 0 0 .9-.85l.45-7.5"></path>
    <path d="M6.5 7v4"></path>
    <path d="M9.5 7v4"></path>
  </svg>`;
}

function paperclipIcon(): string {
  return `<svg class="agd-attachment-icon" viewBox="0 0 16 16" aria-hidden="true">
    <path d="M11.4 4.9 5.9 10.4a1.8 1.8 0 0 0 2.6 2.6l5.1-5.1a3.1 3.1 0 0 0-4.4-4.4L3.6 8.4a4.4 4.4 0 0 0 6.2 6.2l2.5-2.5"></path>
  </svg>`;
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function copyText(value: string, button?: HTMLButtonElement): Promise<void> {
  try {
    await navigator.clipboard.writeText(value);
  } catch {
    const area = document.createElement('textarea');
    area.value = value;
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.append(area);
    area.select();
    document.execCommand('copy');
    area.remove();
  }
  if (!button) return;
  const previous = button.innerHTML;
  button.innerHTML = checkIcon();
  button.disabled = true;
  window.setTimeout(() => {
    button.innerHTML = previous || copyIcon();
    button.disabled = false;
  }, 1200);
}

function iconLogo(kind: 'daemon' | 'identity'): string {
  const label = kind === 'daemon' ? 'D' : 'i';
  const color = kind === 'daemon' ? '#3794FF' : '#A371F7';
  return `<span class="agd-logo" style="--c:${color}">${label}</span>`;
}

function daemonLabel(daemon: DaemonInfo): string {
  return daemon.daemon_name ?? daemon.device_name ?? daemon.daemon_id;
}

/**
 * Where a session ran, for the sidebar's second line.
 *
 * A run's ``dir`` is its history key — this workspace's path — so it says
 * nothing useful about a remote run; name the daemon instead.
 */
function sessionLocationLabel(session: ChatSessionInfo): string {
  const daemon = session.daemon_id
    ? boot?.daemons.find((item) => item.daemon_id === session.daemon_id)
    : undefined;
  if (daemon && !isLocalDaemon(daemon)) return daemonLabel(daemon);
  return session.dir ?? '';
}

function agentLabel(runtime: RuntimeInfo): string {
  return runtime.name || runtime.type;
}

/**
 * Working directory for the selected daemon: the host's workspace root.
 *
 * That path only exists on this machine, so it is the default for a local
 * daemon only. A remote daemon's disk is a different machine — starting
 * the picker there would request a path its ``agent_cwd_allowlist``
 * rejects — so the field starts empty and the user picks a directory.
 */
function defaultCwdFor(): string {
  const daemon = boot?.daemons.find((item) => item.daemon_id === selectedDaemonId);
  if (daemon && !isLocalDaemon(daemon)) return '';
  return boot?.workspaceRoot || boot?.workspaceFolders[0] || '';
}

function syncWorkspaceRoot(): void {
  if (agentCwdTouched || activeSessionId || currentJob) return;
  agentCwd = defaultCwdFor();
}

function identityLabel(identity: IdentityWithBindings): string {
  return identity.name || identity.did;
}

function selectedDaemon(): DaemonInfo | undefined {
  return boot?.daemons.find((daemon) => daemon.daemon_id === selectedDaemonId && daemon.online);
}

function agentsForDaemon(): RuntimeInfo[] {
  if (!boot || !selectedDaemonId) return [];
  return boot.runtimes.filter((runtime) => runtime.daemon_id === selectedDaemonId);
}

function selectedAgent(): RuntimeInfo | undefined {
  return agentsForDaemon().find((runtime) => runtime.type === selectedProvider);
}

function identitiesForSelection(): IdentityWithBindings[] {
  if (!boot) return [];
  // Bindings are optional routing preferences. A selected identity may
  // run on any live daemon/provider the user chooses.
  return (boot.identities ?? [])
    .filter((identity) => identity.status === 'active')
    .map((identity) => ({
      ...identity,
      capabilities: identity.capabilities ?? [],
      bindings: identity.bindings ?? [],
    }));
}

function applyChatSelection(selection: ChatSelection): void {
  if (busy !== 'idle') return;
  const daemonChanged = selectedDaemonId !== selection.daemonId;
  const providerChanged = selectedProvider !== (selection.provider ?? null);
  if (!daemonChanged && !providerChanged) return;

  openDropdown = null;
  selectedDaemonId = selection.daemonId;
  selectedProvider = selection.provider ?? null;
  selectedIdentityDid = '';
  normalizeInitialSelection();
  clearConversation();
  renderSidebar();
  renderMessages();
  renderControls();
  if (daemonChanged) void loadSessions();
}

function normalizeInitialSelection(): void {
  if (!boot) return;
  if (boot.selection) {
    selectedDaemonId = boot.selection.daemonId;
    selectedProvider = boot.selection.provider ?? null;
    selectedIdentityDid = '';
  }
  const daemons = (boot.daemons ?? []).filter((daemon) => daemon.online);
  if (!selectedDaemonId || !daemons.some((daemon) => daemon.daemon_id === selectedDaemonId)) {
    const withAgent = daemons.find((daemon) =>
      (boot?.runtimes ?? []).some((runtime) => runtime.daemon_id === daemon.daemon_id),
    );
    selectedDaemonId = withAgent?.daemon_id ?? daemons[0]?.daemon_id ?? null;
    selectedProvider = null;
    selectedIdentityDid = '';
  }

  const agents = agentsForDaemon();
  if (!selectedProvider || !agents.some((runtime) => runtime.type === selectedProvider)) {
    selectedProvider = agents[0]?.type ?? null;
    selectedIdentityDid = '';
  }

  const identities = identitiesForSelection();
  if (!selectedIdentityDid || !identities.some((identity) => identity.did === selectedIdentityDid)) {
    selectedIdentityDid = (
      identities.find((identity) =>
        identity.bindings.some(
          (binding) =>
            binding.is_default
            && binding.daemon_id === selectedDaemonId
            && binding.provider === selectedProvider,
        ),
      )
      ?? identities.find((identity) =>
        identity.bindings.some(
          (binding) =>
            binding.daemon_id === selectedDaemonId
            && binding.provider === selectedProvider,
        ),
      )
      ?? identities.find((identity) =>
        identity.bindings.some((binding) => binding.is_default),
      )
      ?? identities[0]
    )?.did ?? '';
  }

  syncWorkspaceRoot();
}

function canSend(): boolean {
  const daemon = selectedDaemon();
  // A remote daemon needs an explicit directory (its default cwd is often
  // unrelated to the workspace): the working-directory field must be filled.
  const needsCwd = Boolean(daemon && !isLocalDaemon(daemon));
  return Boolean(
    boot?.state === 'online' &&
      daemon &&
      selectedAgent() &&
      (!needsCwd || agentCwd.trim() !== '') &&
      busy === 'idle' &&
      (!currentJob || currentJob.status === 'waiting_input'),
  );
}

function render(): void {
  renderFrame();
  renderSidebar();
  renderMessages();
  renderControls();
  renderDeleteModal();
}

function renderPreservingComposer(): void {
  const previous = document.getElementById('agd-chat-input') as HTMLTextAreaElement | null;
  const value = previous?.value ?? '';
  const selectionStart = previous?.selectionStart ?? 0;
  const selectionEnd = previous?.selectionEnd ?? 0;
  render();
  const next = document.getElementById('agd-chat-input') as HTMLTextAreaElement | null;
  if (next) {
    next.value = value;
    next.setSelectionRange(selectionStart, selectionEnd);
  }
}

function renderFrame(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  if (loadingBoot && !boot) {
    root.innerHTML = `<div class="agd-chat-loading">${tr('chat.loading')}</div>`;
    return;
  }
  root.innerHTML = `
  <div
    class="agd-chat ${sidebarCollapsed ? 'is-collapsed' : ''}"
    style="--agd-chat-sidebar-width:${sidebarWidth}px"
  >
    <header class="agd-chat-head">
      <div class="agd-chat-brand"><span>AGT</span><small>${tr('app.subtitle')}</small></div>
      <nav class="agd-modes" aria-label="AGT modes">
        <button type="button" class="agd-mode agd-mode-active" data-mode="chat" aria-current="page">${tr('mode.chat')}</button>
        <button type="button" class="agd-mode" data-mode="sop" title="${tr('mode.sop')}">${tr('mode.sop')}</button>
        <button type="button" class="agd-mode" data-mode="goal" title="${tr('mode.goal')}">${tr('mode.goal')}</button>
      </nav>
      <div class="agd-chat-tools">
        <button type="button" id="agd-chat-lang" class="agd-icon-btn">${tr('settings.langToggle')}</button>
        <button type="button" id="agd-chat-settings" class="agd-icon-btn" title="${tr('chat.openSettings')}">⚙</button>
      </div>
    </header>
    <aside id="agd-chat-sidebar" class="agd-chat-side"></aside>
    <div
      id="agd-chat-side-boundary"
      class="agd-chat-side-boundary"
      role="separator"
      aria-orientation="vertical"
      title="${tr('chat.toggleSidebar')}"
    >
      <button
        type="button"
        id="agd-chat-sidebar-toggle"
        class="agd-sidebar-toggle"
        aria-expanded="${sidebarCollapsed ? 'false' : 'true'}"
        aria-label="${tr('chat.toggleSidebar')}"
        title="${tr('chat.toggleSidebar')}"
      >${sidebarCollapsed ? '›' : '‹'}</button>
    </div>
    <section class="agd-chat-main">
      <div id="agd-messages" class="agd-messages"></div>
      <footer id="agd-chat-composer" class="agd-composer">
        <div class="agd-drop-hint">${tr('chat.attachmentDrop')}</div>
        <div id="agd-chat-status" class="agd-chat-status"></div>
        <div id="agd-chat-attachments" class="agd-attachments">${attachmentChipsHtml(attachments)}</div>
        <div class="agd-composer-row">
          <div class="agd-composer-tools">
            <button type="button" id="agd-chat-attach" class="agd-icon-btn" title="${tr('chat.attachFiles')}" aria-label="${tr('chat.attachFiles')}">${paperclipIcon()}</button>
            <input id="agd-chat-file-input" type="file" multiple hidden />
          </div>
          <textarea id="agd-chat-input" rows="3" placeholder="${tr('chat.placeholder')}"></textarea>
          <div id="agd-chat-controls" class="agd-chat-controls"></div>
        </div>
      </footer>
    </section>
  </div>
  <div id="agd-chat-modal-root"></div>`;
  wireFrame();
}

function selectorHtml(kind: 'daemon' | 'agent' | 'identity'): string {
  const open = openDropdown === kind;
  let label: string;
  let leading: string;
  let options: string;
  if (kind === 'daemon') {
    const selected = selectedDaemon();
    leading = iconLogo('daemon');
    label = selected ? daemonLabel(selected) : tr('chat.selectDaemon');
    options = (boot?.daemons ?? [])
      .filter((daemon) => daemon.online)
      .map((daemon) => `
        <button type="button" class="agd-option ${daemon.daemon_id === selectedDaemonId ? 'is-selected' : ''}" data-kind="daemon" data-value="${esc(daemon.daemon_id)}">
          ${iconLogo('daemon')}<span>${esc(daemonLabel(daemon))}</span>
          <small>${esc(daemon.daemon_id)}</small>
        </button>
      `)
      .join('');
  } else if (kind === 'agent') {
    const selected = selectedAgent();
    leading = selected ? providerLogo(selected.type) : iconLogo('identity');
    label = selected ? agentLabel(selected) : tr('chat.selectAgent');
    options = agentsForDaemon()
      .map((runtime) => `
        <button type="button" class="agd-option ${runtime.type === selectedProvider ? 'is-selected' : ''}" data-kind="agent" data-value="${esc(runtime.type)}">
          ${providerLogo(runtime.type)}<span>${esc(agentLabel(runtime))}</span>
          <small>${esc(runtime.version)}</small>
        </button>
      `)
      .join('');
  } else {
    const identities = identitiesForSelection();
    const selected = identities.find((identity) => identity.did === selectedIdentityDid);
    leading = iconLogo('identity');
    label = selected ? identityLabel(selected) : selectedIdentityDid || tr('chat.identityNone');
    options = `
      <button type="button" class="agd-option ${!selectedIdentityDid ? 'is-selected' : ''}" data-kind="identity" data-value="">
        ${iconLogo('identity')}<span>${tr('chat.identityNone')}</span>
      </button>
      ${identities.map((identity) => `
        <button type="button" class="agd-option ${identity.did === selectedIdentityDid ? 'is-selected' : ''}" data-kind="identity" data-value="${esc(identity.did)}">
          ${iconLogo('identity')}<span>${esc(identityLabel(identity))}</span>
          <small>${esc(identity.did)}</small>
        </button>
      `).join('')}
    `;
  }
  return `
  <div class="agd-select ${open ? 'is-open' : ''}">
    <button type="button" class="agd-select-trigger" data-toggle="${kind}">
      ${leading}<span class="agd-select-label">${esc(label)}</span><small>▾</small>
    </button>
    ${open ? `<div class="agd-select-menu">${options || `<div class="agd-select-empty">${kind === 'agent' ? tr('chat.noAgents') : tr('chat.noDaemon')}</div>`}</div>` : ''}
  </div>
  `;
}

function renderSidebar(): void {
  const oldSidebar = document.getElementById('agd-chat-sidebar');
  if (!oldSidebar) return;
  // Replace the shell instead of replacing only innerHTML: prior versions of
  // this element may already have a delegated click listener.
  const sidebar = oldSidebar.cloneNode(false) as HTMLElement;
  oldSidebar.replaceWith(sidebar);
  if (!sidebar) return;
  const sessionItems = sessions
    .map((session) => {
      const location = sessionLocationLabel(session);
      return `
      <div class="agd-session ${session.job_id === activeSessionId ? 'is-selected' : ''}" data-job-id="${esc(session.job_id)}" data-session-id="${esc(session.session_id)}" data-dir="${esc(session.dir ?? '')}">
        <button type="button" class="agd-session-main">
          <span class="agd-session-line">
            ${providerLogo(session.provider)}
            <strong>${esc(firstLine(session.first_prompt) || session.session_id)}</strong>
          </span>
          <small>${esc(session.last_seen_at)}${location ? ` · ${esc(location)}` : ''}</small>
        </button>
        <button
          type="button"
          class="agd-session-delete"
          data-delete-job-id="${esc(session.job_id)}"
          title="${tr('chat.deleteSession')}"
          aria-label="${tr('chat.deleteSession')}: ${esc(firstLine(session.first_prompt) || session.session_id)}"
        >${trashIcon()}</button>
      </div>
    `;
    })
    .join('');

  sidebar.innerHTML = `
  <div class="agd-side-section">
    <div class="agd-side-title">
      <span>${tr('chat.newChat')}</span>
        <button type="button" id="agd-chat-new" class="agd-mini-btn" ${busy === 'idle' ? '' : 'disabled'}>+</button>
    </div>
    <label class="agd-side-label">${tr('chat.daemon')}</label>
    ${selectorHtml('daemon')}
    <label class="agd-side-label">${tr('chat.agent')}</label>
    ${selectorHtml('agent')}
    <label class="agd-side-label">${tr('chat.identity')}</label>
    ${selectorHtml('identity')}
    <label class="agd-side-label">${tr('chat.workingDir')}</label>
    ${bridge.canBrowseDirectories
      ? `<div class="agd-cwd-row">
      <span class="agd-cwd-path agd-mono" title="${esc(agentCwd)}">${esc(agentCwd || tr('chat.noWorkingDir'))}</span>
      <button type="button" id="agd-chat-pick-dir" class="agd-cwd-pick" title="${tr('chat.pickDir')}">${tr('chat.pickDir')}</button>
    </div>`
      : `<input id="agd-chat-cwd" class="agd-input agd-mono" value="${esc(agentCwd)}" placeholder="${tr('chat.workdirPh')}" autocomplete="off" spellcheck="false">`}
  </div>
  <div class="agd-side-section agd-side-sessions">
    <div class="agd-side-title">
      <span>${tr('chat.sessions')}</span>
      <button type="button" id="agd-sessions-refresh" class="agd-mini-btn" title="${tr('chat.refresh')}">↻</button>
    </div>
    <div class="agd-session-list">
      ${sessionsLoading ? `<div class="agd-side-empty">${tr('chat.loading')}</div>` : sessionItems || `<div class="agd-side-empty">${tr('chat.noSessions')}</div>`}
    </div>
  </div>
  ${bridge.isMock ? '<div class="agd-side-empty">browser preview · mock data</div>' : ''}
  `;
  wireSidebar();
}

/**
 * Merge event batches by ``seq`` (idempotent — snapshots, live batches and
 * reconnects may all carry overlapping ranges) and keep them ordered.
 */
function mergeAgentEvents(
  existing: AgentEvent[] | undefined,
  incoming: unknown,
): AgentEvent[] {
  const bySeq = new Map<number, AgentEvent>();
  for (const event of existing ?? []) bySeq.set(event.seq, event);
  if (Array.isArray(incoming)) {
    for (const raw of incoming) {
      if (!raw || typeof raw !== 'object') continue;
      const event = raw as AgentEvent;
      if (typeof event.seq !== 'number' || typeof event.kind !== 'string') continue;
      bySeq.set(event.seq, event);
    }
  }
  return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
}

/**
 * Splice structured events back into the plain-text stream.
 *
 * ``text_offset`` is non-decreasing per Step, so clamping to the already
 * placed length keeps a late/duplicated batch from reordering text. A
 * thinking block is merged by ``block_id``; a tool call by ``tool_use_id``.
 */
function buildBlocks(text: string, events: AgentEvent[]): ChatBlock[] {
  const blocks: ChatBlock[] = [];
  const thinkingAt = new Map<string, number>();
  const toolAt = new Map<string, number>();
  let placed = 0;

  const pushText = (chunk: string): void => {
    if (chunk) blocks.push({ type: 'text', text: chunk });
  };

  for (const event of [...events].sort((a, b) => a.seq - b.seq)) {
    const rawOffset = Number(event.text_offset);
    const offset = Number.isFinite(rawOffset)
      ? Math.min(Math.max(rawOffset, placed), text.length)
      : placed;
    pushText(text.slice(placed, offset));
    placed = offset;

    switch (event.kind) {
      case 'thinking_start': {
        const key = String(event.block_id ?? `thinking-${event.seq}`);
        thinkingAt.set(key, blocks.length);
        blocks.push({ type: 'thinking', key, text: '', done: false });
        break;
      }
      case 'thinking_delta': {
        const key = String(event.block_id ?? 'thinking');
        let index = thinkingAt.get(key);
        if (index === undefined) {
          index = blocks.length;
          thinkingAt.set(key, index);
          blocks.push({ type: 'thinking', key, text: '', done: false });
        }
        const block = blocks[index];
        if (block.type === 'thinking') block.text += String(event.text ?? '');
        break;
      }
      case 'thinking_end': {
        const key = String(event.block_id ?? 'thinking');
        const index = thinkingAt.get(key);
        const block = index === undefined ? undefined : blocks[index];
        if (block && block.type === 'thinking') block.done = true;
        break;
      }
      case 'tool_use_start': {
        const toolUseId = String(event.tool_use_id ?? event.block_id ?? event.seq);
        toolAt.set(toolUseId, blocks.length);
        blocks.push({
          type: 'tool_use',
          key: toolUseId,
          toolUseId,
          name: String(event.name ?? 'tool'),
          done: false,
        });
        break;
      }
      case 'tool_use_end': {
        const toolUseId = String(event.tool_use_id ?? event.block_id ?? '');
        let index = toolAt.get(toolUseId);
        if (index === undefined) {
          index = blocks.length;
          toolAt.set(toolUseId, index);
          blocks.push({
            type: 'tool_use',
            key: toolUseId,
            toolUseId,
            name: String(event.name ?? 'tool'),
            done: false,
          });
        }
        const block = blocks[index];
        if (block.type === 'tool_use') {
          block.input = event.input;
          block.done = true;
          if (!block.name || block.name === 'tool') {
            block.name = String(event.name ?? block.name);
          }
        }
        break;
      }
      case 'tool_result': {
        blocks.push({
          type: 'tool_result',
          toolUseId: String(event.tool_use_id ?? ''),
          content: String(event.content ?? ''),
          isError: event.is_error === true,
        });
        break;
      }
      default:
        // ``usage`` and unknown kinds are not transcript blocks.
        break;
    }
  }
  pushText(text.slice(placed));
  return blocks;
}

/** One-line summary of a tool input — what the call is actually doing. */
function toolInputSummary(input: unknown): string {
  if (!input || typeof input !== 'object') return '';
  const record = input as Record<string, unknown>;
  for (const key of ['command', 'file_path', 'path', 'pattern', 'query', 'url', 'description']) {
    const value = record[key];
    if (typeof value === 'string' && value.trim()) {
      const flat = value.trim().replace(/\s+/g, ' ');
      return `${key}=${flat.length > 100 ? `${flat.slice(0, 99)}…` : flat}`;
    }
  }
  return '';
}

function renderBlocks(blocks: ChatBlock[]): string {
  return blocks.map((block) => {
    if (block.type === 'text') {
      return `<div class="agd-md">${renderMarkdown(block.text)}</div>`;
    }
    if (block.type === 'thinking') {
      const label = esc(tr(block.done ? 'chat.thinking' : 'chat.thinkingActive'));
      return `<details class="agd-thinking"><summary>💭 ${label}</summary><pre class="agd-thinking-body">${esc(block.text)}</pre></details>`;
    }
    if (block.type === 'tool_use') {
      const summary = toolInputSummary(block.input);
      const head = `🔧 <span class="agd-tool-name">${esc(block.name || 'tool')}</span>${
        summary ? `<span class="agd-tool-summary">${esc(summary)}</span>` : ''
      }`;
      if (block.input === undefined) {
        return `<div class="agd-tool">${head}</div>`;
      }
      let pretty = '';
      try {
        pretty = JSON.stringify(block.input, null, 2);
      } catch {
        pretty = String(block.input);
      }
      return `<details class="agd-tool"><summary>${head}</summary><pre class="agd-tool-json">${esc(pretty)}</pre></details>`;
    }
    const marker = block.isError ? '✗' : '↳';
    const label = esc(tr(block.isError ? 'chat.toolFailed' : 'chat.toolResult'));
    const body = block.content.trim() ? `<pre class="agd-tool-json">${esc(block.content)}</pre>` : '';
    return `<details class="agd-tool-result${block.isError ? ' is-error' : ''}"><summary>${marker} ${label}</summary>${body}</details>`;
  }).join('');
}

/** The turn aggregate: ``done``/status usage first, else the ``usage`` event. */
function turnUsage(message: ChatMessage): AgentUsage | null {
  if (message.usage) return message.usage;
  const events = message.events ?? [];
  for (let i = events.length - 1; i >= 0; i -= 1) {
    const event = events[i];
    if (event.kind !== 'usage') continue;
    if (event.scope === 'message') continue;
    return event as unknown as AgentUsage;
  }
  return null;
}

function formatTokens(value: number): string {
  return value.toLocaleString('en-US');
}

/** ``↑in ↓out · cache hit% · $cost`` for one finished turn. */
function usageFooter(usage: AgentUsage | null): string {
  if (!usage) return '';
  const cacheRead = Number(usage.cache_read_input_tokens ?? 0);
  const cacheCreate = Number(usage.cache_creation_input_tokens ?? 0);
  const billedInput = Number(usage.input_tokens ?? 0) + cacheRead + cacheCreate;
  const output = Number(usage.output_tokens ?? 0);
  const parts = [`↑${formatTokens(billedInput)}`, `↓${formatTokens(output)}`];
  if ((cacheRead || cacheCreate) && billedInput > 0) {
    parts.push(tr('chat.usageCache', { percent: String(Math.round((cacheRead / billedInput) * 100)) }));
  }
  if (typeof usage.cost_usd === 'number') {
    parts.push(`$${usage.cost_usd.toFixed(4)}`);
  }
  const model = usage.model ? ` title="${esc(usage.model)}"` : '';
  return `<div class="agd-usage"${model}>${esc(parts.join(' · '))}</div>`;
}

function assistantBodyHtml(message: ChatMessage): string {
  const events = message.events ?? [];
  const footer = usageFooter(turnUsage(message));
  if (!events.length) {
    const body = message.content
      ? renderMarkdown(message.content)
      : '<span class="agd-dim">…</span>';
    return `<div class="agd-bubble agd-agent-bubble agd-md">${body}</div>${footer}`;
  }
  const blocks = buildBlocks(message.content, events);
  const hasText = blocks.some((block) => block.type === 'text' && block.text.trim());
  const placeholder = !hasText && message.streaming ? '<span class="agd-dim">…</span>' : '';
  return `<div class="agd-bubble agd-agent-bubble agd-rich-bubble">${renderBlocks(blocks)}${placeholder}</div>${footer}`;
}

function renderMessages(): void {
  const container = document.getElementById('agd-messages');
  if (!container) return;
  const nearBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight < 120;
  if (!messages.length) {
    const hint = boot?.state !== 'online'
      ? tr('chat.serverOffline')
      : !selectedDaemon()
        ? tr('chat.noDaemon')
        : !selectedAgent()
          ? tr('chat.noAgents')
          : tr('chat.empty');
    const hasSelection = boot?.state === 'online' && selectedDaemon() && selectedAgent();
    const logo = hasSelection
      ? `<div class="agd-chat-empty-logo">${providerLogo(selectedProvider ?? '')}</div>`
      : '';
    const emptyClass = logo ? 'agd-chat-empty has-logo' : 'agd-chat-empty';
    container.innerHTML = `<div class="${emptyClass}">${logo}<span>${esc(hint)}</span></div>`;
    return;
  }
  container.innerHTML = messages.map((message) => {
    if (message.role === 'user') {
      return `<div class="agd-msg agd-msg-user"><div class="agd-bubble">${esc(message.content)}${messageFilesHtml(message.files)}</div><div class="agd-msg-actions"><button type="button" class="agd-mini-btn" data-copy-message="${message.id}" title="${tr('chat.copyMessage')}" aria-label="${tr('chat.copyMessage')}">${copyIcon()}</button></div></div>`;
    }
    if (message.role === 'system') {
      return `<div class="agd-msg agd-msg-system"><div class="agd-bubble">${esc(message.content)}</div><div class="agd-msg-actions"><button type="button" class="agd-mini-btn" data-copy-message="${message.id}" title="${tr('chat.copyMessage')}" aria-label="${tr('chat.copyMessage')}">${copyIcon()}</button></div></div>`;
    }
    return `
    <div class="agd-msg agd-msg-agent">
      <div class="agd-msg-head">
        ${providerLogo(message.provider ?? '')}
        <span>${esc(message.provider ?? 'agent')}</span>
        ${message.streaming ? '<span class="agd-spinner" aria-hidden="true"></span>' : ''}
      </div>
      ${assistantBodyHtml(message)}
      <div class="agd-msg-actions"><button type="button" class="agd-mini-btn" data-copy-message="${message.id}" title="${tr('chat.copyMessage')}" aria-label="${tr('chat.copyMessage')}">${copyIcon()}</button></div>
    </div>
    `;
  }).join('');
  container.querySelectorAll<HTMLButtonElement>('[data-copy-message]').forEach((button) => {
    button.addEventListener('click', () => {
      const message = messages.find((item) => item.id === Number(button.dataset.copyMessage));
      if (message) void copyText(message.content, button);
    });
  });
  container.querySelectorAll<HTMLPreElement>('.agd-md pre').forEach((code) => {
    const wrapper = document.createElement('div');
    wrapper.className = 'agd-code-block';
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'agd-mini-btn agd-code-copy';
    button.title = tr('chat.copyCode');
    button.setAttribute('aria-label', tr('chat.copyCode'));
    button.innerHTML = copyIcon();
    button.addEventListener('click', () => void copyText(code.textContent ?? '', button));
    code.replaceWith(wrapper);
    wrapper.append(code, button);
  });
  container.querySelectorAll<HTMLAnchorElement>('.agd-md a[href]').forEach((anchor) => {
    anchor.addEventListener('click', (event) => {
      event.preventDefault();
      void bridge.call('openExternal', { url: anchor.href });
    });
  });
  if (nearBottom) container.scrollTop = container.scrollHeight;
}

function renderControls(): void {
  const status = document.getElementById('agd-chat-status');
  const controls = document.getElementById('agd-chat-controls');
  if (!status || !controls) return;
  let stateText = tr('chat.idle');
  if (boot?.state !== 'online') stateText = tr('chat.serverOffline');
  else if (busy === 'sending') stateText = tr('chat.running');
  else if (busy === 'settling') stateText = tr('chat.settling');
  else if (busy === 'cancelling') stateText = tr('chat.cancelling');
  else if (currentJob) stateText = `${tr('chat.currentJob')} · ${esc(currentJob.status)}`;

  status.innerHTML = `<span>${stateText}</span>${notice ? `<span class="agd-chat-notice">${esc(notice)}</span>` : ''}`;
  controls.innerHTML = `
    ${busy !== 'idle' && currentJob?.jobId ? `<button type="button" id="agd-chat-cancel" class="agd-btn">${tr('chat.cancel')}</button>` : ''}
    <button type="button" id="agd-chat-send" class="agd-btn agd-btn-primary" ${canSend() ? '' : 'disabled'}>${busy === 'sending' ? '<span class="agd-spinner" aria-hidden="true"></span>' : ''}${tr('chat.send')}</button>
  `;
  document.getElementById('agd-chat-send')?.addEventListener('click', () => void submit());
  document.getElementById('agd-chat-cancel')?.addEventListener('click', () => void cancelTurn());
}

// ── Attachments (user → agent files) ────────────────────────────────

function attachmentChipsHtml(items: ChatAttachment[]): string {
  return items
    .map((item) => {
      const meta = item.status === 'uploading'
        ? tr('chat.attachmentUploading')
        : item.status === 'error'
          ? '!'
          : formatSize(item.size);
      return `<span class="agd-attachment ${item.status === 'error' ? 'is-error' : ''}" title="${esc(item.error ?? item.name)}">
        ${paperclipIcon()}
        <span class="agd-attachment-name">${esc(item.name)}</span>
        <small class="agd-attachment-size">${esc(meta)}</small>
        <button type="button" class="agd-attachment-remove" data-remove-attachment="${item.id}" title="${tr('chat.attachmentRemove')}" aria-label="${tr('chat.attachmentRemove')}">×</button>
      </span>`;
    })
    .join('');
}

function renderAttachments(): void {
  const container = document.getElementById('agd-chat-attachments');
  if (container) container.innerHTML = attachmentChipsHtml(attachments);
}

/** Chips shown inside a sent message bubble (no remove button). */
function messageFilesHtml(files: ChatMessageFile[] | undefined): string {
  if (!files?.length) return '';
  return `<div class="agd-msg-files">${files
    .map(
      (file) => `<span class="agd-attachment is-static">${paperclipIcon()}<span class="agd-attachment-name">${esc(file.name)}</span><small class="agd-attachment-size">${esc(formatSize(file.size))}</small></span>`,
    )
    .join('')}</div>`;
}

/**
 * Base64-encode in chunks: spreading a whole multi-MB byte array into
 * ``String.fromCharCode`` would blow the argument limit.
 */
async function fileToBase64(file: File): Promise<string> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  const chunk = 0x8000;
  let binary = '';
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunk));
  }
  return btoa(binary);
}

async function addFiles(selected: File[]): Promise<void> {
  if (!selected.length) return;
  const room = MAX_ATTACHMENTS - attachments.length;
  if (room <= 0) {
    notice = tr('chat.attachmentTooMany', { max: MAX_ATTACHMENTS });
    renderControls();
    return;
  }
  for (const file of selected.slice(0, room)) {
    if (file.size > MAX_ATTACHMENT_BYTES) {
      notice = tr('chat.attachmentTooLarge', {
        max: Math.round(MAX_ATTACHMENT_BYTES / (1024 * 1024)),
        name: file.name,
      });
      continue;
    }
    const item: ChatAttachment = {
      id: ++attachmentSeq,
      name: file.name,
      size: file.size,
      mimeType: file.type || '',
      status: 'uploading',
    };
    attachments.push(item);
    void uploadAttachment(item, file);
  }
  if (selected.length > room) {
    notice = tr('chat.attachmentTooMany', { max: MAX_ATTACHMENTS });
  }
  renderAttachments();
  renderControls();
}

/** Upload eagerly so a later "send" never waits on the network. */
async function uploadAttachment(item: ChatAttachment, file: File): Promise<void> {
  try {
    const contentB64 = await fileToBase64(file);
    const info = await bridge.call<InputUploadInfo>('upload', {
      name: file.name,
      mimeType: file.type || null,
      contentB64,
    });
    if (!attachments.includes(item)) return;
    item.uploadId = info.upload_id;
    item.uri = info.uri;
    item.name = info.name || item.name;
    item.mimeType = info.mime_type || item.mimeType;
    item.status = 'ready';
  } catch (err) {
    if (!attachments.includes(item)) return;
    item.status = 'error';
    item.error = err instanceof Error ? err.message : String(err);
  }
  renderAttachments();
  if (item.status === 'ready') renderControls();
}

function removeAttachment(id: number): void {
  const index = attachments.findIndex((item) => item.id === id);
  if (index < 0) return;
  const [item] = attachments.splice(index, 1);
  if (item.uploadId) {
    void bridge.call('deleteUpload', { uploadId: item.uploadId }).catch(() => {});
  }
  renderAttachments();
  renderControls();
}

function resetAttachments(): void {
  attachments = [];
  renderAttachments();
}

function buildMessageParts(prompt: string, ready: ChatAttachment[]): MessagePart[] {
  const parts: MessagePart[] = [];
  if (prompt) parts.push({ type: 'text', text: prompt });
  for (const item of ready) {
    if (!item.uri) continue;
    parts.push({
      type: 'file',
      uri: item.uri,
      name: item.name,
      mime_type: item.mimeType || null,
    });
  }
  return parts;
}

function wireAttachments(): void {
  const fileInput = document.getElementById('agd-chat-file-input') as HTMLInputElement | null;
  document.getElementById('agd-chat-attach')?.addEventListener('click', () => fileInput?.click());
  fileInput?.addEventListener('change', () => {
    const picked = Array.from(fileInput.files ?? []);
    // Reset first so picking the same file again still fires ``change``.
    fileInput.value = '';
    void addFiles(picked);
  });
  document
    .getElementById('agd-chat-attachments')
    ?.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement | null)?.closest<HTMLElement>(
        '[data-remove-attachment]',
      );
      if (button) removeAttachment(Number(button.dataset.removeAttachment));
    });
  const composer = document.getElementById('agd-chat-composer');
  if (!composer) return;
  composer.addEventListener('dragover', (event) => {
    event.preventDefault();
    composer.classList.add('is-dragging');
  });
  composer.addEventListener('dragleave', (event) => {
    const next = event.relatedTarget as Node | null;
    if (!next || !composer.contains(next)) composer.classList.remove('is-dragging');
  });
  composer.addEventListener('drop', (event) => {
    event.preventDefault();
    composer.classList.remove('is-dragging');
    void addFiles(Array.from(event.dataTransfer?.files ?? []));
  });
}

function wireFrame(): void {
  wireAttachments();
  document.getElementById('agd-chat-lang')?.addEventListener('click', async () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    renderPreservingComposer();
    await bridge.call('setLanguage', { language: lang });
  });
  document.getElementById('agd-chat-settings')?.addEventListener('click', () => {
    void bridge.call('openSettings');
  });
  document.querySelector('[data-mode="sop"]')?.addEventListener('click', () => {
    void bridge.call('openSop');
  });
  document.querySelector('[data-mode="goal"]')?.addEventListener('click', () => {
    void bridge.call('openGoal');
  });
  const input = document.getElementById('agd-chat-input') as HTMLTextAreaElement | null;
  input?.addEventListener('paste', (event) => {
    // Screenshots and copied files arrive here; plain text still pastes.
    const files = Array.from(event.clipboardData?.files ?? []);
    if (files.length) void addFiles(files);
  });
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
      if (input) recallPrompt(event, input);
      return;
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void submit();
    }
  });
  wireSidebarBoundary();

  if (!documentClickWired) {
    documentClickWired = true;
    document.addEventListener('click', (event) => {
      const target = event.target as HTMLElement | null;
      if (!target?.closest('.agd-select')) {
        if (openDropdown) {
          openDropdown = null;
          renderSidebar();
        }
      }
    });
  }
}

function wireSidebarBoundary(): void {
  const shell = document.querySelector<HTMLElement>('.agd-chat');
  const boundary = document.getElementById('agd-chat-side-boundary');
  const toggle = document.getElementById('agd-chat-sidebar-toggle');
  if (!shell || !boundary || !toggle) return;

  const renderToggle = () => {
    shell.classList.toggle('is-collapsed', sidebarCollapsed);
    toggle.textContent = sidebarCollapsed ? '›' : '‹';
    toggle.setAttribute('aria-expanded', sidebarCollapsed ? 'false' : 'true');
  };

  toggle.addEventListener('click', () => {
    sidebarCollapsed = !sidebarCollapsed;
    window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, sidebarCollapsed ? '1' : '0');
    renderToggle();
  });

  boundary.addEventListener('pointerdown', (event) => {
    if ((event.target as HTMLElement | null)?.closest('#agd-chat-sidebar-toggle')) return;
    event.preventDefault();
    boundary.setPointerCapture(event.pointerId);
    const wasCollapsed = sidebarCollapsed;
    const startWidth = wasCollapsed ? 0 : sidebarWidth;
    const startX = event.clientX;

    if (wasCollapsed) {
      sidebarCollapsed = false;
      sidebarWidth = Math.max(MIN_SIDEBAR_WIDTH, DEFAULT_SIDEBAR_WIDTH);
      shell.style.setProperty('--agd-chat-sidebar-width', `${sidebarWidth}px`);
      renderToggle();
    }

    const move = (moveEvent: PointerEvent) => {
      sidebarWidth = Math.min(
        MAX_SIDEBAR_WIDTH,
        Math.max(MIN_SIDEBAR_WIDTH, startWidth + moveEvent.clientX - startX),
      );
      shell.style.setProperty('--agd-chat-sidebar-width', `${sidebarWidth}px`);
    };
    const stop = (stopEvent: PointerEvent) => {
      boundary.releasePointerCapture(stopEvent.pointerId);
      boundary.removeEventListener('pointermove', move);
      boundary.removeEventListener('pointerup', stop);
      boundary.removeEventListener('pointercancel', stop);
      window.localStorage.setItem(SIDEBAR_WIDTH_KEY, String(sidebarWidth));
    };
    boundary.addEventListener('pointermove', move);
    boundary.addEventListener('pointerup', stop);
    boundary.addEventListener('pointercancel', stop);
  });
}

function wireSidebar(): void {
  const sidebar = document.getElementById('agd-chat-sidebar');
  sidebar?.addEventListener('click', (event) => {
    const toggle = (event.target as HTMLElement | null)?.closest<HTMLElement>('[data-toggle]');
    if (toggle) {
      const kind = toggle.dataset.toggle as 'daemon' | 'agent' | 'identity';
      openDropdown = openDropdown === kind ? null : kind;
      renderSidebar();
      return;
    }
    const option = (event.target as HTMLElement | null)?.closest<HTMLElement>('.agd-option');
    if (option) {
      selectOption(option.dataset.kind ?? '', option.dataset.value ?? '');
      return;
    }
    const deleteButton = (event.target as HTMLElement | null)?.closest<HTMLElement>(
      '[data-delete-job-id]',
    );
    if (deleteButton) {
      const jobId = deleteButton.dataset.deleteJobId ?? '';
      const session = sessions.find((item) => item.job_id === jobId);
      if (session) openDeleteSessionDialog(session);
      return;
    }
    const sessionMain = (event.target as HTMLElement | null)?.closest<HTMLElement>(
      '.agd-session-main',
    );
    if (sessionMain) {
      const session = sessionMain.closest<HTMLElement>('.agd-session');
      if (session?.dataset.jobId) void selectSession(session.dataset.jobId, session.dataset.dir);
    }
  });
  document.getElementById('agd-chat-new')?.addEventListener('click', () => {
    if (busy !== 'idle') return;
    clearConversation();
    agentCwdTouched = false;
    syncWorkspaceRoot();
    renderSidebar();
    renderMessages();
    renderControls();
  });
  document.getElementById('agd-sessions-refresh')?.addEventListener('click', () => {
    void loadSessions();
  });
  document.getElementById('agd-chat-cwd')?.addEventListener('change', (event) => {
    if (busy !== 'idle') return;
    agentCwd = (event.target as HTMLInputElement).value.trim();
    agentCwdTouched = true;
    if (currentJob) {
      currentJob = null;
      activeSessionId = null;
      renderControls();
    }
  });
  document.getElementById('agd-chat-pick-dir')?.addEventListener('click', () => {
    const daemon = selectedDaemon();
    if (!daemon) return;
    openDirPicker({
      language: lang,
      title: tr('chat.pickDirTitle'),
      daemonId: isLocalDaemon(daemon) ? null : daemon.daemon_id,
      startPath: agentCwd || null,
      onSelect: (path) => {
        agentCwd = path;
        // A remote pick must survive later ``workspaceRoot`` syncs — that
        // path exists on another machine, not in this workspace. A local
        // pick keeps tracking the open folder, as before.
        if (!isLocalDaemon(daemon)) agentCwdTouched = true;
        renderSidebar();
        renderControls();
      },
    });
  });
}

function selectOption(kind: string, value: string): void {
  if (busy !== 'idle') return;
  openDropdown = null;
  if (kind === 'daemon' && value !== selectedDaemonId) {
    selectedDaemonId = value;
    selectedProvider = null;
    selectedIdentityDid = '';
    // A directory is machine-specific: drop the previous daemon's choice so
    // the next sync starts from the new daemon's default (empty for remote).
    agentCwdTouched = false;
    normalizeInitialSelection();
    clearConversation();
    syncWorkspaceRoot();
    void loadSessions();
  } else if (kind === 'agent' && value !== selectedProvider) {
    selectedProvider = value;
    selectedIdentityDid = '';
    normalizeInitialSelection();
    clearConversation();
  } else if (kind === 'identity' && value !== selectedIdentityDid) {
    selectedIdentityDid = value;
    if (currentJob) clearConversation();
  } else {
    renderSidebar();
    return;
  }
  renderSidebar();
  renderMessages();
  renderControls();
}

function clearConversation(): void {
  currentJob = null;
  activeSessionId = null;
  messages = [];
  notice = null;
  busy = 'idle';
}

async function loadSessions(): Promise<void> {
  if (!selectedDaemonId) {
    sessions = [];
    renderSidebar();
    return;
  }
  const seq = ++sessionsRequestSeq;
  sessionsLoading = true;
  renderSidebar();
  try {
    const res = await bridge.call<{ sessions: ChatSessionInfo[] }>(
      'sessions',
      { daemonId: selectedDaemonId },
    );
    if (seq === sessionsRequestSeq) {
      sessions = res.sessions;
    }
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    renderControls();
  } finally {
    if (seq === sessionsRequestSeq) sessionsLoading = false;
    renderSidebar();
  }
}

function renderDeleteModal(): void {
  const modalRoot = document.getElementById('agd-chat-modal-root');
  if (!modalRoot) return;
  if (!pendingDeleteSession) {
    modalRoot.innerHTML = '';
    return;
  }

  const label = firstLine(pendingDeleteSession.first_prompt) || pendingDeleteSession.session_id;
  modalRoot.innerHTML = `
  <div class="agd-modal-backdrop" role="presentation">
    <div class="agd-modal" role="dialog" aria-modal="true" aria-labelledby="agd-chat-delete-title">
      <h3 id="agd-chat-delete-title">${tr('chat.deleteSessionTitle')}</h3>
      <p>${tr('chat.deleteSessionBody')}</p>
      <div class="agd-modal-target">${esc(label)}</div>
      <div class="agd-modal-actions">
        <button type="button" id="agd-chat-delete-cancel" class="agd-btn" ${deletingSession ? 'disabled' : ''}>
          ${tr('chat.cancelDelete')}
        </button>
        <button type="button" id="agd-chat-delete-confirm" class="agd-btn agd-modal-danger" ${deletingSession ? 'disabled' : ''}>
          ${deletingSession ? '<span class="agd-spinner" aria-hidden="true"></span>' : ''}${tr('chat.delete')}
        </button>
      </div>
    </div>
  </div>`;

  document.getElementById('agd-chat-delete-cancel')?.addEventListener('click', () => {
    if (!deletingSession) closeDeleteSessionDialog();
  });
  document.getElementById('agd-chat-delete-confirm')?.addEventListener('click', () => {
    void confirmDeleteSession();
  });
  modalRoot.querySelector('.agd-modal-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !deletingSession) closeDeleteSessionDialog();
  });
  modalRoot.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !deletingSession) closeDeleteSessionDialog();
  });
  requestAnimationFrame(() => {
    if (deletingSession) return;
    document.getElementById('agd-chat-delete-cancel')?.focus();
  });
}

function openDeleteSessionDialog(session: ChatSessionInfo): void {
  if (deletingSession) return;
  pendingDeleteSession = session;
  // Delete under the daemon that owns the session, not whichever daemon the
  // user has selected for new chats.
  pendingDeleteDaemonId = session.daemon_id ?? selectedDaemonId;
  renderDeleteModal();
}

function closeDeleteSessionDialog(): void {
  if (deletingSession) return;
  pendingDeleteSession = null;
  pendingDeleteDaemonId = null;
  renderDeleteModal();
}

async function confirmDeleteSession(): Promise<void> {
  if (!pendingDeleteSession || deletingSession) return;
  const target = pendingDeleteSession;
  const daemonId = pendingDeleteDaemonId ?? '';
  deletingSession = true;
  renderDeleteModal();
  try {
    const result = await bridge.call<DeleteSessionResult>('deleteSession', {
      jobId: target.job_id,
      provider: target.provider,
      daemonId,
      finalizeActive: true,
    });
    sessions = result.sessions;
    if (activeSessionId === target.job_id) clearConversation();
    pendingDeleteSession = null;
    pendingDeleteDaemonId = null;
    notice = tr('chat.deleteSessionDone');
  } catch (err) {
    pendingDeleteSession = null;
    pendingDeleteDaemonId = null;
    notice = tr('chat.deleteSessionFailed', {
      message: err instanceof Error ? err.message : String(err),
    });
  } finally {
    deletingSession = false;
    renderDeleteModal();
    renderSidebar();
    renderMessages();
    renderControls();
  }
}

async function selectSession(jobId: string, dir?: string): Promise<void> {
  if (busy !== 'idle') return;
  activeSessionId = jobId;
  sessionsLoading = true;
  renderSidebar();
  try {
    const res = await bridge.call<SelectSessionResult>('selectSession', { jobId, dir: dir || null });
    const status = res.jobStatus.status;
    clearConversation();
    activeSessionId = jobId;
    selectedDaemonId = res.resolved.daemon_id ?? selectedDaemonId;
    selectedProvider = res.resolved.provider;
    selectedIdentityDid = res.resolved.identity_did ?? '';
    agentCwd = res.resolved.dir ?? agentCwd;

    if (res.history?.messages.length) {
      messages = res.history.messages.map((message) => ({
        id: ++messageSeq,
        role: message.role === 'user'
          ? 'user'
          : message.role === 'assistant'
            ? 'assistant'
            : 'system',
        content: message.content,
        provider: message.role === 'assistant' ? res.resolved.provider : undefined,
      }));
    } else {
      const existing = sessions.find((session) => session.job_id === jobId);
      if (existing) {
        messages = [{ id: ++messageSeq, role: 'user', content: existing.first_prompt }];
      }
    }
    if (res.historyError) {
      notice = tr('chat.historyUnavailable', { error: res.historyError });
    }

    if (status === 'waiting_input') {
      currentJob = {
        jobId: res.resolved.job_id,
        taskId: res.resolved.task_id,
        status,
        sessionId: res.resolved.session_id,
      };
    } else {
      notice = tr('chat.notResumable', { status });
    }
  } catch (err) {
    clearConversation();
    notice = err instanceof Error ? err.message : String(err);
  } finally {
    sessionsLoading = false;
    renderSidebar();
    renderMessages();
    renderControls();
  }
}

async function submit(): Promise<void> {
  if (!canSend()) return;
  const textarea = document.getElementById('agd-chat-input') as HTMLTextAreaElement | null;
  const typed = textarea?.value.trim() ?? '';
  const failed = attachments.find((item) => item.status === 'error');
  if (failed) {
    notice = tr('chat.attachmentFailed', { error: failed.error ?? failed.name });
    renderControls();
    return;
  }
  if (attachments.some((item) => item.status === 'uploading')) {
    notice = tr('chat.attachmentStillUploading');
    renderControls();
    return;
  }
  const ready = attachments.filter((item) => item.status === 'ready');
  if (!typed && !ready.length) return;
  // Attachment-only turns still need a prompt for the run title / legacy
  // readers; the same text leads the structured message.
  const prompt = typed || tr('chat.attachmentOnlyPrompt');
  const parts = ready.length ? buildMessageParts(prompt, ready) : undefined;
  const message: ChatInputMessage | undefined =
    parts && parts.some((part) => part.type === 'file')
      ? { role: 'user', parts }
      : undefined;
  const provider = selectedProvider;
  if (!provider) return;

  if (textarea) textarea.value = '';
  messages.push({
    id: ++messageSeq,
    role: 'user',
    content: prompt,
    files: ready.length ? ready.map(({ name, size }) => ({ name, size })) : undefined,
  });
  if (typed && promptHistory[promptHistory.length - 1] !== typed) {
    promptHistory.push(typed);
  }
  promptHistoryIndex = null;
  promptDraft = '';
  resetAttachments();
  messages.push({
    id: ++messageSeq,
    role: 'assistant',
    content: '',
    provider,
    streaming: true,
  });
  busy = 'sending';
  notice = null;
  if (currentJob) currentJob.status = 'running';
  renderMessages();
  renderControls();

  try {
    const result = await bridge.call<MockSendResult>('send', {
      prompt,
      daemonId: selectedDaemonId,
      provider,
      identityDid: selectedIdentityDid || null,
      agentCwd: agentCwd || null,
      currentJobId: currentJob?.jobId ?? null,
      ...(message ? { message } : {}),
    });
    if (bridge.isMock && result.events) {
      for (const event of result.events) applyChatEvent(event);
    }
  } catch (err) {
    finishWithError(err instanceof Error ? err.message : String(err));
  }
}

async function cancelTurn(): Promise<void> {
  if (!currentJob?.jobId || busy === 'idle') return;
  busy = 'cancelling';
  renderControls();
  try {
    await bridge.call('stop', { jobId: currentJob.jobId });
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    busy = lastAssistant()?.streaming ? 'sending' : 'settling';
    renderControls();
  }
}

function lastAssistant(): ChatMessage | undefined {
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === 'assistant') return messages[i];
  }
  return undefined;
}

function applyChatEvent(payload: ChatEvent): void {
  const kind = String(payload.kind ?? '');
  if (kind === 'started') {
    currentJob = {
      jobId: String(payload.job_id ?? ''),
      taskId: String(payload.task_id ?? ''),
      status: String(payload.status ?? 'pending'),
    };
    renderControls();
    return;
  }
  if (kind === 'snapshot' || kind === 'delta') {
    const assistant = lastAssistant();
    if (assistant) {
      assistant.content = kind === 'snapshot'
        ? String(payload.stdout ?? '')
        : assistant.content + String(payload.chunk ?? '');
      if (kind === 'snapshot') {
        // Reconnect / resumed turn: the snapshot replays stored events and
        // may carry the turn usage summary.
        assistant.events = mergeAgentEvents([], payload.events);
        const usage = payload.usage;
        if (usage && typeof usage === 'object') assistant.usage = usage as AgentUsage;
      }
      renderMessages();
    }
    return;
  }
  if (kind === 'agent_events') {
    const assistant = lastAssistant();
    if (assistant) {
      assistant.events = mergeAgentEvents(assistant.events, payload.events);
      renderMessages();
    }
    return;
  }
  if (kind === 'done') {
    const assistant = lastAssistant();
    if (assistant) {
      assistant.content = String(payload.stdout ?? '');
      assistant.streaming = false;
      const usage = payload.usage;
      if (usage && typeof usage === 'object') assistant.usage = usage as AgentUsage;
    }
    if (currentJob) {
      if (payload.session_id) {
        currentJob.sessionId = String(payload.session_id);
      }
      activeSessionId = currentJob.jobId;
    }
    busy = 'settling';
    renderMessages();
    renderControls();
    return;
  }
  if (kind === 'settled') {
    busy = 'idle';
    if (payload.jobStatus && typeof payload.jobStatus === 'object') {
      const status = payload.jobStatus as { status?: string };
      if (currentJob && status.status) currentJob.status = status.status;
    }
    if (Array.isArray(payload.sessions)) {
      sessions = payload.sessions as ChatSessionInfo[];
    }
    if (payload.sessionsError) {
      notice = String(payload.sessionsError);
    }
    renderSidebar();
    renderMessages();
    renderControls();
    return;
  }
  if (kind === 'streamFallback') {
    notice = tr('chat.streamFallback');
    renderControls();
    return;
  }
  if (kind === 'cancelling') {
    busy = 'cancelling';
    renderControls();
    return;
  }
  if (kind === 'error') {
    finishWithError(String(payload.message ?? 'unknown error'));
  }
}

function finishWithError(message: string): void {
  const assistant = lastAssistant();
  if (assistant?.streaming) {
    assistant.streaming = false;
    if (!assistant.content) assistant.content = message;
  }
  busy = 'idle';
  notice = tr('chat.turnError', { message });
  renderMessages();
  renderControls();
}

function firstLine(value: string): string {
  return value.split(/\r?\n/, 1)[0] ?? '';
}

async function main(): Promise<void> {
  boot = await bridge.call<ChatBoot>('boot');
  lang = boot.language;
  loadingBoot = false;
  normalizeInitialSelection();
  render();
  void loadSessions();

  bridge.onEvent((event, payload) => {
    if (event === 'chat') {
      applyChatEvent(payload as ChatEvent);
    } else if (event === 'refresh') {
      const next = payload as ChatBoot;
      const previousDaemon = selectedDaemonId;
      const previousProvider = selectedProvider;
      const previousIdentity = selectedIdentityDid;
      boot = next;
      lang = next.language;
      selectedDaemonId = previousDaemon;
      selectedProvider = previousProvider;
      selectedIdentityDid = previousIdentity;
      normalizeInitialSelection();
      renderPreservingComposer();
      if (previousDaemon !== selectedDaemonId) void loadSessions();
    } else if (event === 'selection') {
      applyChatSelection(payload as ChatSelection);
    } else if (event === 'workspaceRoot') {
      if (!boot) return;
      boot.workspaceRoot = String(payload ?? '');
      syncWorkspaceRoot();
      renderSidebar();
    } else if (event === 'language') {
      lang = payload as UiLanguage;
      renderPreservingComposer();
    }
  });
}

void main();
