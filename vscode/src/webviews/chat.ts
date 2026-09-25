import './chat.css';
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
}

interface ChatMessage {
  id: number;
  role: 'user' | 'assistant' | 'system';
  content: string;
  provider?: string;
  streaming?: boolean;
  stderr?: string;
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

type ChatEvent = Record<string, unknown> & { kind?: string };
type MockSendResult = { events?: ChatEvent[] };
type BusyState = 'idle' | 'sending' | 'settling' | 'cancelling';

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
let sessions: ChatSessionInfo[] = [];
let sessionsLoading = false;
let activeSessionId: string | null = null;
let currentJob: ActiveJob | null = null;
let messages: ChatMessage[] = [];
let busy: BusyState = 'idle';
let notice: string | null = null;
let openDropdown: 'daemon' | 'agent' | 'identity' | null = null;
let messageSeq = 0;
let loadingBoot = true;
let documentClickWired = false;

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

function normalizeInitialSelection(): void {
  if (!boot) return;
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

  if (!agentCwd) {
    agentCwd = boot.workspaceFolders[0] ?? '';
  }
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
  <div class="agd-chat">
    <header class="agd-chat-head">
      <div class="agd-chat-brand"><span>AGT</span><small>${tr('app.subtitle')}</small></div>
      <nav class="agd-modes" aria-label="AGT modes">
        <button type="button" class="agd-mode agd-mode-active">${tr('mode.chat')}</button>
        <button type="button" class="agd-mode" id="agd-mode-sop" title="${tr('mode.sop')}">${tr('mode.sop')}</button>
        <button type="button" class="agd-mode" id="agd-mode-goal" title="${tr('mode.goal')}">${tr('mode.goal')}</button>
      </nav>
      <div class="agd-chat-tools">
        <button type="button" id="agd-chat-lang" class="agd-icon-btn">${tr('settings.langToggle')}</button>
        <button type="button" id="agd-chat-settings" class="agd-icon-btn" title="${tr('chat.openSettings')}">⚙</button>
      </div>
    </header>
    <aside id="agd-chat-sidebar" class="agd-chat-side"></aside>
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
  </div>`;
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
      <button type="button" class="agd-session ${session.session_id === activeSessionId ? 'is-selected' : ''}" data-session-id="${esc(session.session_id)}">
        <span class="agd-session-line">
          ${providerLogo(session.provider)}
          <strong>${esc(firstLine(session.first_prompt) || session.session_id)}</strong>
        </span>
        <small>${esc(session.last_seen_at)}${session.dir ? ` · ${esc(session.dir)}` : ''}</small>
      </button>
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
      return `<div class="agd-msg agd-msg-user"><div class="agd-bubble">${esc(message.content)}</div></div>`;
    }
    if (message.role === 'system') {
      return `<div class="agd-msg agd-msg-system"><div class="agd-bubble">${esc(message.content)}</div></div>`;
    }
    return `
    <div class="agd-msg agd-msg-agent">
      <div class="agd-msg-head">
        ${providerLogo(message.provider ?? '')}
        <span>${esc(message.provider ?? 'agent')}</span>
        ${message.streaming ? '<span class="agd-spinner" aria-hidden="true"></span>' : ''}
      </div>
      <div class="agd-bubble agd-agent-bubble">${esc(message.content || '…')}</div>
      ${message.stderr ? `<div class="agd-agent-stderr">${esc(message.stderr)}</div>` : ''}
    </div>
    `;
  }).join('');
  container.scrollTop = container.scrollHeight;
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
  document.getElementById('agd-mode-sop')?.addEventListener('click', () => {
    void bridge.call('openSop');
  });
  document.getElementById('agd-mode-goal')?.addEventListener('click', () => {
    void bridge.call('openGoal');
  });
  const input = document.getElementById('agd-chat-input') as HTMLTextAreaElement | null;
  input?.addEventListener('keydown', (event) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      void submit();
    }
  });
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
    const session = (event.target as HTMLElement | null)?.closest<HTMLElement>('.agd-session');
    if (session?.dataset.sessionId) {
      void selectSession(session.dataset.sessionId);
    }
  });
  document.getElementById('agd-chat-new')?.addEventListener('click', () => {
    if (busy !== 'idle') return;
    clearConversation();
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
    await bridge.call('cancel', { jobId: currentJob.jobId });
  } catch (err) {
    notice = err instanceof Error ? err.message : String(err);
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
      assistant.stderr = payload.stderr ? String(payload.stderr) : undefined;
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
    } else if (event === 'language') {
      lang = payload as UiLanguage;
      renderPreservingComposer();
    }
  });
}

void main();
