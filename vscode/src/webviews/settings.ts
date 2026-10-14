import './mode-switcher.css';
import './styles.css';
import { t, type MessageKey } from '../core/i18n';
import type { DaemonConfigInfo, DaemonInfo, UiLanguage } from '../core/types';
import { bridge, type BootInfo } from './bridge';

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

function providerColor(provider: string): string {
  return PROVIDER_COLORS[provider.toLowerCase()] ?? '#6E7681';
}

function esc(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function logo(provider: string): string {
  const letter = provider.charAt(0).toUpperCase();
  return `<span class="agd-logo" style="--c:${providerColor(provider)}">${esc(letter)}</span>`;
}

let boot: BootInfo | undefined;
let lang: UiLanguage = 'zh';
let busyAction: string | null = null;
let daemonForm: {
  open: boolean;
  busy: boolean;
  error: string | null;
  result: { command: string; daemonsPath: string } | null;
  draft: { name: string; url: string; capabilities: string };
} = { open: false, busy: false, error: null, result: null, draft: { name: '', url: '', capabilities: '' } };
let copiedAt = 0;

interface DaemonConfigDraft {
  maxConcurrent: string;
  agentTimeout: string;
  workDir: string;
  agentCwdAllowlist: string;
}

let daemonConfig: {
  daemonId: string | null;
  open: boolean;
  loading: boolean;
  saving: boolean;
  error: string | null;
  saved: boolean;
  info: DaemonConfigInfo | null;
  draft: DaemonConfigDraft | null;
} = {
  daemonId: null,
  open: false,
  loading: false,
  saving: false,
  error: null,
  saved: false,
  info: null,
  draft: null,
};

function tr(key: MessageKey, vars?: Record<string, string | number>): string {
  return t(lang, key, vars);
}

function statusPill(): string {
  if (!boot) return '';
  if (boot.state === 'online' && busyAction !== 'testConnection') {
    return `<span class="agd-pill agd-pill-ok">${tr('settings.connected', { ms: boot.latencyMs ?? '?' })}</span>`;
  }
  if (boot.state === 'starting' || busyAction === 'startServer') {
    return `<span class="agd-pill agd-pill-warn">${tr('status.starting')}…</span>`;
  }
  if (busyAction === 'stopServer') {
    return `<span class="agd-pill agd-pill-warn">${tr('status.offline')}…</span>`;
  }
  return `<span class="agd-pill agd-pill-err">${tr('settings.disconnected')}</span>`;
}

function spinner(): string {
  return `<span class="agd-spinner" aria-hidden="true"></span>`;
}

function configValueText(value: unknown): string {
  if (Array.isArray(value)) {
    return value.map((item) => String(item)).join('\n');
  }
  return String(value ?? '');
}

function configDraft(info: DaemonConfigInfo): DaemonConfigDraft {
  return {
    maxConcurrent: configValueText(info.settings.max_concurrent.value),
    agentTimeout: configValueText(info.settings.agent_timeout.value),
    workDir: configValueText(info.settings.work_dir.value),
    agentCwdAllowlist: configValueText(info.settings.agent_cwd_allowlist.value),
  };
}

function renderDaemonForm(): string {
  if (daemonForm.result) {
    const copied = Date.now() - copiedAt < 1600;
    return `
      <div class="agd-daemon-form">
        <p class="agd-form-ok">${tr('settings.addDaemonOk', { path: esc(daemonForm.result.daemonsPath) })}</p>
        <pre class="agd-cmd"><code>${esc(daemonForm.result.command)}</code></pre>
        <div class="agd-actions">
          <button type="button" id="agd-copy-cmd" class="agd-btn">${copied ? tr('settings.copied') : tr('settings.copy')}</button>
          <button type="button" id="agd-daemon-close" class="agd-btn">${tr('settings.cancel')}</button>
        </div>
      </div>`;
  }
  return `
    <div class="agd-daemon-form">
      <label class="agd-field">${tr('settings.daemonName')}<input id="agd-daemon-name" class="agd-input" value="${esc(daemonForm.draft.name)}" autocomplete="off"></label>
      <label class="agd-field">${tr('settings.daemonUrl')}<input id="agd-daemon-url" class="agd-input agd-mono" placeholder="ws://192.168.1.24:8422" value="${esc(daemonForm.draft.url)}" autocomplete="off"></label>
      <label class="agd-field">${tr('settings.daemonCaps')}<input id="agd-daemon-caps" class="agd-input" placeholder="${tr('settings.daemonCapsPh')}" value="${esc(daemonForm.draft.capabilities)}" autocomplete="off"></label>
      ${daemonForm.error ? `<p class="agd-form-err">${esc(daemonForm.error)}</p>` : ''}
      <div class="agd-actions">
        <button type="button" id="agd-daemon-submit" class="agd-btn agd-btn-primary" ${daemonForm.busy ? 'disabled' : ''}>${daemonForm.busy ? spinner() : ''}${tr('settings.addDaemonSubmit')}</button>
        <button type="button" id="agd-daemon-cancel" class="agd-btn" ${daemonForm.busy ? 'disabled' : ''}>${tr('settings.cancel')}</button>
      </div>
    </div>`;
}

function renderDaemonConfigField(
  key: keyof DaemonConfigInfo['settings'],
  label: string,
  control: string,
): string {
  const info = daemonConfig.info;
  if (!info) return '';
  const setting = info.settings[key];
  const source =
    setting.source === 'env' && setting.env_name
      ? `env ${setting.env_name}`
      : setting.source;
  const saved = setting.saved === null ? '—' : configValueText(setting.saved).replaceAll('\n', ', ');
  const fallback = setting.default === null
    ? '—'
    : configValueText(setting.default).replaceAll('\n', ', ');

  return `
    <label class="agd-field agd-daemon-config-field">
      <span>${label}</span>
      ${control}
      <span class="agd-daemon-config-meta">
        ${tr('settings.daemonConfigSource')}: <strong>${esc(source)}</strong>
        · ${tr('settings.daemonConfigSaved')}: <span class="agd-mono">${esc(saved)}</span>
        · ${tr('settings.daemonConfigDefault')}: <span class="agd-mono">${esc(fallback)}</span>
      </span>
      ${setting.source === 'env' && setting.env_name
        ? `<span class="agd-daemon-config-warning">${tr('settings.daemonConfigEnvOverride', { name: setting.env_name })}</span>`
        : ''}
    </label>`;
}

function renderDaemonConfigModal(): string {
  if (!daemonConfig.open) return '';
  const daemon = boot?.daemons.find((item) => item.daemon_id === daemonConfig.daemonId);
  const daemonLabel = daemon?.daemon_name || daemon?.device_name || daemonConfig.daemonId || '';

  let body = '';
  if (daemonConfig.loading) {
    body = `<div class="agd-daemon-config-state">${spinner()}${tr('settings.daemonConfigLoading')}</div>`;
  } else if (!daemonConfig.info) {
    body = `<p class="agd-form-err">${esc(daemonConfig.error ?? '')}</p>`;
  } else {
    const draft = daemonConfig.draft;
    body = `
      ${renderDaemonConfigField(
        'max_concurrent',
        tr('settings.daemonConfigMaxConcurrent'),
        `<input id="agd-daemon-config-concurrency" class="agd-input" type="number" min="1" step="1" value="${esc(draft?.maxConcurrent ?? '')}">`,
      )}
      ${renderDaemonConfigField(
        'agent_timeout',
        tr('settings.daemonConfigAgentTimeout'),
        `<input id="agd-daemon-config-timeout" class="agd-input" type="number" min="1" step="1" value="${esc(draft?.agentTimeout ?? '')}">`,
      )}
      ${renderDaemonConfigField(
        'work_dir',
        tr('settings.daemonConfigWorkDir'),
        `<input id="agd-daemon-config-workdir" class="agd-input agd-mono" value="${esc(draft?.workDir ?? '')}">`,
      )}
      ${renderDaemonConfigField(
        'agent_cwd_allowlist',
        tr('settings.daemonConfigAllowlist'),
        `<textarea id="agd-daemon-config-allowlist" class="agd-input agd-daemon-config-textarea agd-mono" rows="4" placeholder="${tr('settings.daemonConfigAllowlistPh')}">${esc(draft?.agentCwdAllowlist ?? '')}</textarea>`,
      )}
      ${daemonConfig.error ? `<p class="agd-form-err">${esc(daemonConfig.error)}</p>` : ''}
      ${daemonConfig.saved ? `<p class="agd-form-ok">${tr('settings.daemonConfigRestart')}</p>` : ''}`;
  }

  return `
  <div class="agd-modal-backdrop" id="agd-daemon-config-backdrop">
    <section class="agd-modal agd-daemon-config" role="dialog" aria-modal="true" aria-labelledby="agd-daemon-config-title">
      <header class="agd-modal-head">
        <div>
          <h3 id="agd-daemon-config-title">${tr('settings.daemonConfig')}</h3>
          <p class="agd-dim">${esc(daemonLabel)} · <span class="agd-mono">${esc(daemonConfig.daemonId ?? '')}</span></p>
        </div>
        <button type="button" id="agd-daemon-config-close" class="agd-btn" ${daemonConfig.saving ? 'disabled' : ''}>${tr('settings.daemonConfigClose')}</button>
      </header>
      ${daemonConfig.info ? `<p class="agd-note agd-dim agd-mono">${tr('settings.daemonConfigFile', { path: daemonConfig.info.config_file })}</p>` : ''}
      ${body}
      <div class="agd-actions agd-modal-actions">
        <span class="agd-flex"></span>
        <button type="button" id="agd-daemon-config-save" class="agd-btn agd-btn-primary" ${!daemonConfig.info || daemonConfig.loading || daemonConfig.saving ? 'disabled' : ''}>
          ${daemonConfig.saving ? spinner() + tr('settings.daemonConfigSaving') : tr('settings.daemonConfigSave')}
        </button>
      </div>
    </section>
  </div>`;
}

function providerChips(daemon: DaemonInfo): string {
  return daemon.providers
    .map(
      (p) =>
        `<span class="agd-chip">${logo(p.provider)}${esc(p.provider)}</span>`,
    )
    .join('');
}

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  if (!boot) {
    root.innerHTML = `<div class="agd-loading">…</div>`;
    return;
  }
  const currentBoot = boot;
  const daemonRows = currentBoot.daemons
    .map(
      (d) => `
      <tr class="${d.online ? 'is-clickable' : ''}" data-config-daemon-id="${esc(d.daemon_id)}">
        <td><span class="agd-mono">${esc(d.daemon_id)}</span></td>
        <td>${esc(d.device_name)}</td>
        <td><span class="agd-chips">${providerChips(d)}</span></td>
        <td><span class="agd-pill ${d.online ? 'agd-pill-ok' : 'agd-pill-err'}">${d.online ? 'online' : 'offline'}</span></td>
        <td class="agd-dim">${esc(d.last_seen)}</td>
        <td class="agd-table-action">
          <button
            type="button"
            class="agd-btn agd-daemon-config-button"
            data-config-daemon-id="${esc(d.daemon_id)}"
            ${d.online ? '' : 'disabled'}
            title="${d.daemon_id === currentBoot.localDaemonId ? tr('settings.daemonConfigOpen') : tr('settings.daemonConfigRemote')}"
          >⚙</button>
        </td>
      </tr>`,
    )
    .join('');

  root.innerHTML = `
  <div class="agd-wrap">
    <header class="agd-head">
      <div class="agd-head-main">
        <div class="agd-title">${tr('app.title')} <span class="agd-sub">${tr('app.subtitle')}</span></div>
        <nav class="agd-modes" aria-label="AGT modes">
          <button type="button" class="agd-mode" data-mode="chat">${tr('mode.chat')}</button>
          <button type="button" class="agd-mode" data-mode="sop">${tr('mode.sop')}</button>
          <button type="button" class="agd-mode" data-mode="goal">${tr('mode.goal')}</button>
        </nav>
      </div>
      <div class="agd-head-tools">
        <button type="button" id="agd-lang" class="agd-lang">${tr('settings.langToggle')}</button>
      </div>
    </header>

    <section class="agd-card">
      <div class="agd-card-head">
        <span>${tr('settings.connection')}</span>
        <span class="agd-flex"></span>
        ${statusPill()}
      </div>
      <label class="agd-field agd-field-row">
        <span>${tr('settings.serverUrl')}</span>
        <input id="agd-url" class="agd-input agd-mono" value="${esc(boot.serverUrl)}">
        <button type="button" id="agd-test" class="agd-btn" ${busyAction ? 'disabled' : ''}>${busyAction === 'testConnection' ? spinner() : ''}${tr('settings.test')}</button>
      </label>
      <div class="agd-switch-group">
        <label class="agd-switch-row">
          <span class="agd-switch"><input id="agt-auto" type="checkbox" ${boot.autoStart ? 'checked' : ''}><i></i></span>
          <span>${tr('settings.autoStart')}</span>
        </label>
        <label class="agd-switch-row">
          <span class="agd-switch"><input id="agt-auto-daemon" type="checkbox" ${boot.autoStartDaemon ? 'checked' : ''}><i></i></span>
          <span>${tr('settings.autoStartDaemon')}</span>
        </label>
      </div>
      <div class="agd-actions">
        <button type="button" id="agd-start" class="agd-btn agd-btn-primary" ${boot.state === 'online' || busyAction ? 'disabled' : ''}>${busyAction === 'startServer' ? spinner() + tr('status.starting') : tr('settings.startServer')}</button>
        <button type="button" id="agd-stop" class="agd-btn" ${boot.state !== 'online' || busyAction ? 'disabled' : ''}>${busyAction === 'stopServer' ? spinner() : ''}${tr('settings.stopServer')}</button>
        <span class="agd-flex"></span>
        <span class="agd-dim agd-daemon-state">${boot.daemonRunning ? tr('settings.daemonRunning') : tr('settings.daemonStopped')}</span>
        <button type="button" id="agd-daemon-start" class="agd-btn" ${boot.daemonRunning || busyAction ? 'disabled' : ''}>${busyAction === 'startDaemon' ? spinner() : ''}${tr('settings.startDaemon')}</button>
        <button type="button" id="agd-daemon-stop" class="agd-btn" ${!boot.daemonRunning || busyAction ? 'disabled' : ''}>${busyAction === 'stopDaemon' ? spinner() : ''}${tr('settings.stopDaemon')}</button>
      </div>
    </section>

    <section class="agd-card">
      <div class="agd-card-head">
        <span>${tr('settings.daemons')}</span>
        <span class="agd-flex"></span>
        <button type="button" id="agd-add-daemon" class="agd-btn">${tr('settings.addDaemon')}</button>
      </div>
      ${daemonForm.open ? renderDaemonForm() : ''}
      <table class="agd-table">
        <thead>
          <tr><th>ID</th><th>${tr('settings.host')}</th><th>${tr('settings.providers')}</th><th>${tr('settings.status')}</th><th>${tr('settings.lastSeen')}</th><th></th></tr>
        </thead>
        <tbody>${daemonRows || `<tr><td colspan="6" class="agd-dim">${tr('tree.noDaemons')}</td></tr>`}</tbody>
      </table>
      <p class="agd-note agd-dim">${tr('settings.daemonHint', { command: `agt daemon start --server ${boot.serverUrl}` })}</p>
      <p class="agd-note agd-dim">${tr('settings.daemonConfigLocalOnly')}</p>
    </section>

    ${bridge.isMock ? `<p class="agd-mock-note">browser preview · mock data</p>` : ''}
  </div>
  ${renderDaemonConfigModal()}`;

  wireEvents();
}

async function call(method: string, params?: Record<string, unknown>): Promise<void> {
  if (busyAction) return;
  busyAction = method;
  render();
  try {
    const res = await bridge.call<Record<string, unknown>>(method, params);
    if (res && typeof res === 'object' && 'daemons' in res) {
      applyBoot({ ...boot, ...res } as BootInfo);
    }
  } catch (err) {
    console.error(`[agt-settings] ${method} failed:`, err);
  } finally {
    busyAction = null;
    render();
  }
}

function daemonConfigError(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  if (message.includes('remote-daemon-config-unsupported')) {
    return tr('settings.daemonConfigRemote');
  }
  if (/\bHTTP 404\b/.test(message)) {
    return tr('settings.daemonConfigOldServer');
  }
  return message;
}

async function openDaemonConfig(daemonId: string): Promise<void> {
  if (daemonConfig.loading || daemonConfig.saving) return;
  daemonConfig = {
    daemonId,
    open: true,
    loading: true,
    saving: false,
    error: null,
    saved: false,
    info: null,
    draft: null,
  };
  if (daemonId !== boot?.localDaemonId) {
    daemonConfig.loading = false;
    daemonConfig.error = tr('settings.daemonConfigRemote');
    render();
    return;
  }

  render();
  try {
    const info = await bridge.call<DaemonConfigInfo>('daemonConfigShow', { daemonId });
    daemonConfig.info = info;
    daemonConfig.draft = configDraft(info);
  } catch (err) {
    daemonConfig.error = daemonConfigError(err);
  } finally {
    daemonConfig.loading = false;
    render();
  }
}

function sameConfigValue(left: unknown, right: unknown): boolean {
  if (Array.isArray(left) && Array.isArray(right)) {
    return left.length === right.length && left.every((item, index) => item === right[index]);
  }
  return left === right;
}

async function saveDaemonConfig(): Promise<void> {
  const info = daemonConfig.info;
  const draft = daemonConfig.draft;
  if (!info || !draft || daemonConfig.loading || daemonConfig.saving) return;

  const concurrency = Number(draft.maxConcurrent);
  const timeout = Number(draft.agentTimeout);
  const allowlist = draft.agentCwdAllowlist
    .split(/\r?\n/)
    .map((item) => item.trim())
    .filter(Boolean);

  if (!Number.isInteger(concurrency) || concurrency < 1) {
    daemonConfig.error = tr('settings.daemonConfigPositiveInteger');
    render();
    return;
  }
  if (!Number.isInteger(timeout) || timeout < 1) {
    daemonConfig.error = tr('settings.daemonConfigPositiveInteger');
    render();
    return;
  }
  if (allowlist.length === 0) {
    daemonConfig.error = tr('settings.daemonConfigAllowlistRequired');
    render();
    return;
  }

  const updates: Record<string, unknown> = {};
  if (!sameConfigValue(concurrency, info.settings.max_concurrent.value)) {
    updates.maxConcurrent = concurrency;
  }
  if (!sameConfigValue(timeout, info.settings.agent_timeout.value)) {
    updates.agentTimeout = timeout;
  }
  if (draft.workDir.trim() !== info.settings.work_dir.value) {
    updates.workDir = draft.workDir.trim();
  }
  if (!sameConfigValue(allowlist, info.settings.agent_cwd_allowlist.value)) {
    updates.agentCwdAllowlist = allowlist;
  }
  if (Object.keys(updates).length === 0) {
    daemonConfig.error = tr('settings.daemonConfigNoChanges');
    daemonConfig.saved = false;
    render();
    return;
  }

  daemonConfig.saving = true;
  daemonConfig.error = null;
  daemonConfig.saved = false;
  render();
  try {
    const next = await bridge.call<DaemonConfigInfo>('daemonConfigSet', {
      daemonId: daemonConfig.daemonId,
      ...updates,
    });
    daemonConfig.info = next;
    daemonConfig.draft = configDraft(next);
    daemonConfig.saved = true;
  } catch (err) {
    daemonConfig.error = daemonConfigError(err);
  } finally {
    daemonConfig.saving = false;
    render();
  }
}

function applyBoot(next: BootInfo): void {
  boot = next;
  lang = next.language;
}

function wireEvents(): void {
  document.querySelector('[data-mode="chat"]')?.addEventListener('click', () => void bridge.call('openChat'));
  document.querySelector('[data-mode="sop"]')?.addEventListener('click', () => void bridge.call('openSop'));
  document.querySelector('[data-mode="goal"]')?.addEventListener('click', () => void bridge.call('openGoal'));
  document.getElementById('agd-lang')?.addEventListener('click', async () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    render();
    await bridge.call('setConfig', { language: lang });
  });
  document.getElementById('agd-test')?.addEventListener('click', () => void call('testConnection'));
  document.getElementById('agd-start')?.addEventListener('click', () => void call('startServer'));
  document.getElementById('agd-stop')?.addEventListener('click', () => void call('stopServer'));
  document.getElementById('agt-auto')?.addEventListener('change', (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    if (boot) boot.autoStart = checked;
    void bridge.call('setConfig', { autoStart: checked });
  });
  document.getElementById('agt-auto-daemon')?.addEventListener('change', (e) => {
    const checked = (e.target as HTMLInputElement).checked;
    if (boot) boot.autoStartDaemon = checked;
    void bridge.call('setConfig', { autoStartDaemon: checked });
  });
  document.getElementById('agd-daemon-start')?.addEventListener('click', () => void call('startDaemon'));
  document.getElementById('agd-daemon-stop')?.addEventListener('click', () => void call('stopDaemon'));
  document.querySelectorAll<HTMLElement>('tr[data-config-daemon-id]').forEach((row) => {
    row.addEventListener('click', () => {
      if (row.querySelector('button:disabled')) return;
      void openDaemonConfig(row.dataset.configDaemonId ?? '');
    });
  });
  document.getElementById('agd-url')?.addEventListener('change', (e) => {
    const url = (e.target as HTMLInputElement).value.trim();
    if (boot) boot.serverUrl = url;
    void bridge.call('setConfig', { serverUrl: url });
  });
  document.getElementById('agd-daemon-config-close')?.addEventListener('click', () => {
    if (!daemonConfig.saving) {
      daemonConfig.open = false;
      render();
    }
  });
  document.getElementById('agd-daemon-config-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !daemonConfig.saving) {
      daemonConfig.open = false;
      render();
    }
  });
  const draftInputs: [string, keyof DaemonConfigDraft][] = [
    ['agd-daemon-config-concurrency', 'maxConcurrent'],
    ['agd-daemon-config-timeout', 'agentTimeout'],
    ['agd-daemon-config-workdir', 'workDir'],
    ['agd-daemon-config-allowlist', 'agentCwdAllowlist'],
  ];
  for (const [id, key] of draftInputs) {
    document.getElementById(id)?.addEventListener('input', (event) => {
      if (!daemonConfig.draft) return;
      daemonConfig.draft[key] = (event.target as HTMLInputElement | HTMLTextAreaElement).value;
      daemonConfig.saved = false;
    });
  }
  document.getElementById('agd-daemon-config-save')?.addEventListener('click', () => {
    void saveDaemonConfig();
  });
  document.getElementById('agd-add-daemon')?.addEventListener('click', () => {
    daemonForm = { ...daemonForm, open: !daemonForm.open, error: null, result: null };
    render();
  });
  document.getElementById('agd-daemon-cancel')?.addEventListener('click', () => {
    daemonForm = { open: false, busy: false, error: null, result: null, draft: daemonForm.draft };
    render();
  });
  document.getElementById('agd-daemon-close')?.addEventListener('click', () => {
    daemonForm = { open: false, busy: false, error: null, result: null, draft: daemonForm.draft };
    render();
  });
  for (const [id, key] of [
    ['agd-daemon-name', 'name'],
    ['agd-daemon-url', 'url'],
    ['agd-daemon-caps', 'capabilities'],
  ] as const) {
    document.getElementById(id)?.addEventListener('input', (e) => {
      daemonForm.draft[key] = (e.target as HTMLInputElement).value;
    });
  }
  document.getElementById('agd-daemon-submit')?.addEventListener('click', async () => {
    const { name, url, capabilities } = daemonForm.draft;
    if (!name.trim()) {
      daemonForm.error = tr('settings.addDaemonNeedName');
      render();
      return;
    }
    if (!/^[A-Za-z0-9_.-]+$/.test(name.trim())) {
      daemonForm.error = tr('settings.addDaemonBadName');
      render();
      return;
    }
    if (!/^wss?:\/\/\S+$/.test(url.trim())) {
      daemonForm.error = tr('settings.addDaemonBadUrl');
      render();
      return;
    }
    daemonForm.busy = true;
    daemonForm.error = null;
    render();
    try {
      daemonForm.result = await bridge.call<{ command: string; daemonsPath: string }>('addDaemon', {
        name: name.trim(),
        url: url.trim(),
        capabilities,
      });
    } catch (err) {
      const code = err instanceof Error ? err.message : '';
      const errors: Record<string, string> = {
        'duplicate-url': tr('settings.addDaemonDup'),
        'bad-url': tr('settings.addDaemonBadUrl'),
        'name-required': tr('settings.addDaemonNeedName'),
        'bad-name': tr('settings.addDaemonBadName'),
      };
      daemonForm.error = errors[code] ?? `${code || String(err)}`;
    } finally {
      daemonForm.busy = false;
      render();
    }
  });
  document.getElementById('agd-copy-cmd')?.addEventListener('click', async () => {
    const command = daemonForm.result?.command;
    if (!command) {
      return;
    }
    try {
      await navigator.clipboard?.writeText(command);
    } catch {
      // Clipboard may be unavailable in the webview; the command stays selectable.
    }
    copiedAt = Date.now();
    render();
  });
}

async function main(): Promise<void> {
  applyBoot(await bridge.call<BootInfo>('boot'));
  render();
  bridge.onEvent((event, payload) => {
    if (event === 'refresh') {
      applyBoot(payload as BootInfo);
      render();
    }
  });
}

void main();
