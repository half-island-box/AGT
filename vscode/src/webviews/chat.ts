import './mode-switcher.css';
import './chat.css';
import { marked } from 'marked';
import DOMPurify from 'dompurify';
import { t, type MessageKey } from '../core/i18n';
import type {
  ChatSessionInfo,
  DaemonInfo,
  IdentityWithBindings,
  RuntimeInfo,
  SessionHistoryInfo,
  UiLanguage,
} from '../core/types';
import { bridge } from './bridge';

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

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  provider?: string;
  streaming?: boolean;
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

const SIDEBAR_WIDTH_KEY = 'agt.chat.sidebarWidth';
const SIDEBAR_COLLAPSED_KEY = 'agt.chat.sidebarCollapsed';
const DEFAULT_SIDEBAR_WIDTH = 292;
const MIN_SIDEBAR_WIDTH = 180;
const MAX_SIDEBAR_WIDTH = 520;

const PROVIDER_COLORS: Record<string, string> = {
  claude: '#D97757',
  codex: '#10A37F',
  qwen: '#615CED',
  kimi: '#0EA5E9',
  copilot: '#8B5CF6',
  cursor: '#F2F2F2',
  opencode: '#14B8A6',
  openclaw: '#F59E0B',
};

let boot: ChatBoot | undefined;
let lang: UiLanguage = 'zh';
let selectedDaemonId: string | null = null;
let selectedProvider: string | null = null;
let selectedIdentityDid = '';
let agentCwd = '';
let agentCwdTouched = false;
let sessions: ChatSessionInfo[] = [];
let sessionsLoading = false;
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

function providerColor(provider: string): string {
  return PROVIDER_COLORS[provider.toLowerCase()] ?? '#6E7681';
}

function providerLogo(provider: string): string {
  return `<span class="agd-logo" style="--c:${providerColor(provider)}">${esc(provider.charAt(0).toUpperCase())}</span>`;
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

function renderMarkdown(value: string): string {
  const html = marked.parse(value, {
    async: false,
    gfm: true,
    breaks: true,
  }) as string;
  return DOMPurify.sanitize(html, {
    USE_PROFILES: { html: true },
    FORBID_TAGS: ['style', 'form'],
    FORBID_ATTR: ['style'],
  });
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

function agentLabel(runtime: RuntimeInfo): string {
  return runtime.name || runtime.type;
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
  if (!boot || !selectedDaemonId || !selectedProvider) return [];
  return boot.identities.filter((identity) =>
    identity.bindings.some(
      (binding) => binding.daemon_id === selectedDaemonId && binding.provider === selectedProvider,
    ),
  );
}

function syncWorkspaceRoot(): void {
  if (agentCwdTouched || activeSessionId || currentJob) return;
  agentCwd = boot?.workspaceRoot || boot?.workspaceFolders[0] || '';
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
  const daemons = boot.daemons.filter((daemon) => daemon.online);
  if (!selectedDaemonId || !daemons.some((daemon) => daemon.daemon_id === selectedDaemonId)) {
    const withAgent = daemons.find((daemon) =>
      boot?.runtimes.some((runtime) => runtime.daemon_id === daemon.daemon_id),
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
    selectedIdentityDid = identities.find((identity) =>
      identity.bindings.some((binding) => binding.is_default),
    )?.did ?? identities[0]?.did ?? '';
  }

  syncWorkspaceRoot();
}

function canSend(): boolean {
  return Boolean(
    boot?.state === 'online' &&
      selectedDaemon() &&
      selectedAgent() &&
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
      <footer class="agd-composer">
        <div id="agd-chat-status" class="agd-chat-status"></div>
        <div class="agd-composer-row">
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
    .map((session) => `
      <div class="agd-session ${session.session_id === activeSessionId ? 'is-selected' : ''}" data-session-id="${esc(session.session_id)}">
        <button type="button" class="agd-session-main">
          <span class="agd-session-line">
            ${providerLogo(session.provider)}
            <strong>${esc(firstLine(session.first_prompt) || session.session_id)}</strong>
          </span>
          <small>${esc(session.last_seen_at)}${session.dir ? ` · ${esc(session.dir)}` : ''}</small>
        </button>
        <button
          type="button"
          class="agd-session-delete"
          data-delete-session-id="${esc(session.session_id)}"
          title="${tr('chat.deleteSession')}"
          aria-label="${tr('chat.deleteSession')}: ${esc(firstLine(session.first_prompt) || session.session_id)}"
        >${trashIcon()}</button>
      </div>
    `)
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
    <label class="agd-side-label">${tr('chat.workdir')}</label>
    <input id="agd-chat-cwd" class="agd-input agd-mono" value="${esc(agentCwd)}" placeholder="${tr('chat.workdirPh')}" autocomplete="off" spellcheck="false">
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
    container.innerHTML = `<div class="agd-chat-empty">${esc(hint)}</div>`;
    return;
  }
  container.innerHTML = messages.map((message) => {
    if (message.role === 'user') {
      return `<div class="agd-msg agd-msg-user"><div class="agd-bubble">${esc(message.content)}</div><div class="agd-msg-actions"><button type="button" class="agd-mini-btn" data-copy-message="${message.id}" title="${tr('chat.copyMessage')}" aria-label="${tr('chat.copyMessage')}">${copyIcon()}</button></div></div>`;
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
      <div class="agd-bubble agd-agent-bubble agd-md">${message.content ? renderMarkdown(message.content) : '<span class="agd-dim">…</span>'}</div>
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

function wireFrame(): void {
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
      '[data-delete-session-id]',
    );
    if (deleteButton) {
      const sessionId = deleteButton.dataset.deleteSessionId ?? '';
      const session = sessions.find((item) => item.session_id === sessionId);
      if (session) openDeleteSessionDialog(session);
      return;
    }
    const sessionMain = (event.target as HTMLElement | null)?.closest<HTMLElement>(
      '.agd-session-main',
    );
    if (sessionMain) {
      const session = sessionMain.closest<HTMLElement>('.agd-session');
      if (session?.dataset.sessionId) void selectSession(session.dataset.sessionId);
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
}

function selectOption(kind: string, value: string): void {
  if (busy !== 'idle') return;
  openDropdown = null;
  if (kind === 'daemon' && value !== selectedDaemonId) {
    selectedDaemonId = value;
    selectedProvider = null;
    selectedIdentityDid = '';
    normalizeInitialSelection();
    clearConversation();
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
  const daemonId = selectedDaemonId;
  sessionsLoading = true;
  renderSidebar();
  try {
    const res = await bridge.call<{ sessions: ChatSessionInfo[] }>('sessions', { daemonId });
    if (daemonId === selectedDaemonId) {
      sessions = res.sessions;
    }
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
    renderControls();
  } finally {
    sessionsLoading = false;
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
  if (deletingSession || !selectedDaemonId) return;
  pendingDeleteSession = session;
  pendingDeleteDaemonId = selectedDaemonId;
  renderDeleteModal();
}

function closeDeleteSessionDialog(): void {
  if (deletingSession) return;
  pendingDeleteSession = null;
  pendingDeleteDaemonId = null;
  renderDeleteModal();
}

async function confirmDeleteSession(): Promise<void> {
  if (!pendingDeleteSession || !pendingDeleteDaemonId || deletingSession) return;
  const target = pendingDeleteSession;
  const daemonId = pendingDeleteDaemonId;
  deletingSession = true;
  renderDeleteModal();
  try {
    const result = await bridge.call<DeleteSessionResult>('deleteSession', {
      sessionId: target.session_id,
      provider: target.provider,
      daemonId,
      finalizeActive: true,
    });
    sessions = result.sessions;
    if (activeSessionId === target.session_id) clearConversation();
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

async function selectSession(sessionId: string): Promise<void> {
  if (!selectedDaemonId || busy !== 'idle') return;
  activeSessionId = sessionId;
  sessionsLoading = true;
  renderSidebar();
  try {
    const res = await bridge.call<SelectSessionResult>('selectSession', { sessionId });
    const status = res.jobStatus.status;
    clearConversation();
    activeSessionId = sessionId;
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
      const existing = sessions.find((session) => session.session_id === sessionId);
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
  const prompt = textarea?.value.trim() ?? '';
  if (!prompt) return;
  const provider = selectedProvider;
  if (!provider) return;

  if (textarea) textarea.value = '';
  messages.push({ id: ++messageSeq, role: 'user', content: prompt });
  if (promptHistory[promptHistory.length - 1] !== prompt) {
    promptHistory.push(prompt);
  }
  promptHistoryIndex = null;
  promptDraft = '';
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
      renderMessages();
    }
    return;
  }
  if (kind === 'done') {
    const assistant = lastAssistant();
    if (assistant) {
      assistant.content = String(payload.stdout ?? '');
      assistant.streaming = false;
    }
    if (currentJob && payload.session_id) {
      currentJob.sessionId = String(payload.session_id);
      activeSessionId = currentJob.sessionId;
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
