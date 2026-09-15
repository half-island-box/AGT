import './styles.css';
import { t, type MessageKey } from '../core/i18n';
import type { DaemonInfo, UiLanguage } from '../core/types';
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
  const daemonRows = boot.daemons
    .map(
      (d) => `
      <tr>
        <td><span class="agd-mono">${esc(d.daemon_id)}</span></td>
        <td>${esc(d.device_name)}</td>
        <td><span class="agd-chips">${providerChips(d)}</span></td>
        <td><span class="agd-pill ${d.online ? 'agd-pill-ok' : 'agd-pill-err'}">${d.online ? 'online' : 'offline'}</span></td>
        <td class="agd-dim">${esc(d.last_seen)}</td>
      </tr>`,
    )
    .join('');

  root.innerHTML = `
  <div class="agd-wrap">
    <header class="agd-head">
      <div>
        <div class="agd-title">${tr('app.title')} <span class="agd-sub">${tr('app.subtitle')}</span></div>
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
      <label class="agd-switch-row">
        <span class="agd-switch"><input id="agt-auto" type="checkbox" ${boot.autoStart ? 'checked' : ''}><i></i></span>
        <span>${tr('settings.autoStart')}</span>
      </label>
      <div class="agd-actions">
        <button type="button" id="agd-start" class="agd-btn agd-btn-primary" ${boot.state === 'online' || busyAction ? 'disabled' : ''}>${busyAction === 'startServer' ? spinner() + tr('status.starting') : tr('settings.startServer')}</button>
        <button type="button" id="agd-stop" class="agd-btn" ${boot.state !== 'online' || busyAction ? 'disabled' : ''}>${busyAction === 'stopServer' ? spinner() : ''}${tr('settings.stopServer')}</button>
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
          <tr><th>ID</th><th>${tr('settings.host')}</th><th>${tr('settings.providers')}</th><th>${tr('settings.status')}</th><th>${tr('settings.lastSeen')}</th></tr>
        </thead>
        <tbody>${daemonRows || `<tr><td colspan="5" class="agd-dim">${tr('tree.noDaemons')}</td></tr>`}</tbody>
      </table>
      <p class="agd-note agd-dim">${tr('settings.daemonHint', { command: `agt daemon start --server ${boot.serverUrl}` })}</p>
    </section>

    ${bridge.isMock ? `<p class="agd-mock-note">browser preview · mock data</p>` : ''}
  </div>`;

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

function applyBoot(next: BootInfo): void {
  boot = next;
  lang = next.language;
}

function wireEvents(): void {
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
  document.getElementById('agd-url')?.addEventListener('change', (e) => {
    const url = (e.target as HTMLInputElement).value.trim();
    if (boot) boot.serverUrl = url;
    void bridge.call('setConfig', { serverUrl: url });
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
