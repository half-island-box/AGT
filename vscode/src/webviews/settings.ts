import './mode-switcher.css';
import './styles.css';
import { isMissingEndpoint } from '../core/api';
import { t, type MessageKey } from '../core/i18n';
import { providerLogo } from '../core/icons';
import type {
  DaemonConfigInfo,
  DaemonInfo,
  IdentityWithBindings,
  PairingAddDaemonInfo,
  PairingRemoveInfo,
  PairingStateInfo,
  UiLanguage,
} from '../core/types';
import { bridge, type BootInfo } from './bridge';

function esc(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

/*
 * Reverse-dial endpoints are fixed: the Server always dials
 * `ws://<host>:8422/daemon`, so the UI only asks for the host and composes
 * the URL. Accepting a pasted full URL (or `host:port`) keeps the field
 * forgiving without exposing the fixed parts as editable inputs.
 */
const DAEMON_WS_SCHEME = 'ws';
const DAEMON_WS_PORT = 8422;

/** The bare host from whatever the user typed or pasted. */
function daemonHost(raw: string): string {
  let value = raw.trim();
  if (!value) return '';
  value = value.replace(/^wss?:\/\//i, '');
  value = value.replace(/\/.*$/, '');
  const bracketed = value.match(/^(\[[^\]]+\])(?::\d+)?$/);
  if (bracketed) return bracketed[1];
  const hostPort = value.match(/^([^:/]+):\d+$/);
  return hostPort ? hostPort[1] : value;
}

/** `ws://<host>:8422` — empty when no host was entered. */
function daemonUrl(host: string): string {
  const bare = daemonHost(host);
  return bare ? `${DAEMON_WS_SCHEME}://${bare}:${DAEMON_WS_PORT}` : '';
}

function isValidDaemonHost(raw: string): boolean {
  const host = daemonHost(raw);
  return /^[A-Za-z0-9._-]+$/.test(host) || /^\[[0-9A-Fa-f:.]+\]$/.test(host);
}

/**
 * Update the composed-URL hint under a host input without re-rendering (a
 * re-render would drop focus and the caret mid-typing).
 */
function syncDaemonHostHint(inputId: string, raw: string): void {
  const hint = document.getElementById(`${inputId}-hint`);
  if (!hint) return;
  const url = daemonUrl(raw);
  hint.textContent = url ? tr('settings.daemonHostHint', { url }) : '';
}

let boot: BootInfo | undefined;
let lang: UiLanguage = 'zh';
let busyAction: string | null = null;
let pairingCopiedAt = 0;

/**
 * Add-daemon (invite code) state.
 *
 * The flow lives behind the daemons card's "Add daemon" button and is
 * rendered as a modal: form → invite code. There is no separate
 * "Connection & pairing" card anymore.
 */
let pairing: {
  probed: boolean;
  loading: boolean;
  busy: boolean;
  error: string | null;
  addOpen: boolean;
  daemonsPath: string;
  result: {
    daemonId: string;
    added: string;
    command: string;
    restartRequired: boolean;
    restarting: boolean;
  } | null;
  draft: { name: string; host: string; capabilities: string };
} = {
  probed: false,
  loading: false,
  busy: false,
  error: null,
  addOpen: false,
  daemonsPath: '',
  result: null,
  draft: { name: '', host: '', capabilities: '' },
};

/**
 * Per-row "remove" state for the daemons card: the button arms on the
 * first click (``confirmId``) and deletes the target on the second, so a
 * misclick cannot drop a daemon. Kept apart from ``busyAction`` so an
 * in-flight removal disables the table, not the whole page.
 */
let daemonRemove: {
  confirmId: string | null;
  busyId: string | null;
  error: string | null;
  notice: string | null;
} = { confirmId: null, busyId: null, error: null, notice: null };

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

type IdentityModalMode = 'create' | 'view' | 'edit';

interface IdentityDraft {
  name: string;
  slug: string;
  description: string;
  capabilities: string;
}

interface IdentityBindingDraft {
  daemonId: string;
  provider: string;
  priority: string;
  isDefault: boolean;
}

let identityModal: {
  open: boolean;
  mode: IdentityModalMode;
  did: string | null;
  busy: boolean;
  error: string | null;
  draft: IdentityDraft;
  binding: IdentityBindingDraft;
} = {
  open: false,
  mode: 'view',
  did: null,
  busy: false,
  error: null,
  draft: { name: '', slug: '', description: '', capabilities: '' },
  binding: { daemonId: '', provider: '', priority: '0', isDefault: false },
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

/**
 * False once the Server answered 404 for the pairing endpoints. Older
 * Servers predate them; hiding the card beats showing an endpoint error
 * the user cannot act on.
 */
let pairingSupported = true;

function pairingEnabled(): boolean {
  return pairingSupported
    && boot !== undefined
    && boot.state === 'online'
    && boot.capabilities?.pairing !== false;
}

/**
 * Only registered targets can be removed: a runtime-only daemon (no
 * ``url``) has no ``~/.agt/daemons`` entry to delete, and the local
 * daemon is controlled by the Server/daemon switches instead.
 */
function removableDaemon(d: DaemonInfo): boolean {
  return pairingEnabled() && Boolean(d.url) && d.daemon_id !== boot?.localDaemonId;
}

function pairingErrorText(err: unknown): string {
  const message = err instanceof Error ? err.message : String(err);
  return tr('settings.pairingError', { message });
}

async function copyText(value: string): Promise<void> {
  try {
    await navigator.clipboard?.writeText(value);
  } catch {
    // Clipboard may be unavailable in the webview; the text stays selectable.
  }
}

/**
 * Probe the pairing API once per online Server; called from applyBoot.
 * Kept only to detect Servers that predate it (404 → the Add-daemon
 * button stays hidden instead of failing on submit).
 */
async function loadPairing(): Promise<void> {
  if (!pairingEnabled() || pairing.loading) {
    return;
  }
  pairing.loading = true;
  try {
    const state = await bridge.call<PairingStateInfo>('pairingState');
    pairing.daemonsPath = state.daemons_path;
    pairing.probed = true;
  } catch (err) {
    if (isMissingEndpoint(err)) {
      pairingSupported = false;
    }
  } finally {
    pairing.loading = false;
    render();
  }
}

async function submitPairingForm(): Promise<void> {
  const { name, host, capabilities } = pairing.draft;
  if (!name.trim()) {
    pairing.error = tr('settings.addDaemonNeedName');
    render();
    return;
  }
  if (!/^[A-Za-z0-9_.-]+$/.test(name.trim())) {
    pairing.error = tr('settings.addDaemonBadName');
    render();
    return;
  }
  if (!isValidDaemonHost(host)) {
    pairing.error = tr('settings.addDaemonBadUrl');
    render();
    return;
  }
  const url = daemonUrl(host);
  pairing.busy = true;
  pairing.error = null;
  render();
  try {
    const res = await bridge.call<PairingAddDaemonInfo>('pairingAddDaemon', {
      name: name.trim(),
      url,
      capabilities,
    });
    pairing.result = {
      daemonId: res.daemon_id,
      added: res.added,
      command: res.command,
      restartRequired: res.restart_required,
      restarting: false,
    };
    pairing.daemonsPath = res.daemons_path;
    pairing.draft = { name: '', host: '', capabilities: '' };
    void call('refresh');
  } catch (err) {
    pairing.error = pairingErrorText(err);
  } finally {
    pairing.busy = false;
    render();
  }
}

/**
 * Make the Server re-read its daemon target file so a freshly registered
 * target starts being dialed now (``POST /api/daemons/refresh``) instead
 * of after a restart. Updates the daemon list as a side effect.
 */
async function connectPairingTarget(): Promise<void> {
  if (!pairing.result || pairing.result.restarting) {
    return;
  }
  pairing.result = { ...pairing.result, restarting: true };
  pairing.error = null;
  render();
  try {
    await bridge.call('refreshDaemons');
    pairing.result = { ...pairing.result, restarting: false, restartRequired: false };
  } catch (err) {
    pairing.error = pairingErrorText(err);
    pairing.result = { ...pairing.result, restarting: false };
  } finally {
    render();
    void call('refresh');
  }
}

/**
 * Remove a registered target from the daemons file and re-sync the
 * Server's live links, so the row disappears now rather than after a
 * restart.
 *
 * The Server's in-memory target list is only re-read on
 * ``POST /api/daemons/refresh``; without that step the row would linger
 * (the DELETE response still says ``restart_required``). The refresh is
 * best-effort: an older Server without the endpoint keeps the old
 * behaviour instead of failing the removal that already happened.
 */
async function removeDaemonTarget(daemonId: string, url?: string | null): Promise<void> {
  if (daemonRemove.busyId) return;
  daemonRemove.busyId = daemonId;
  daemonRemove.error = null;
  daemonRemove.notice = null;
  render();
  try {
    const res = await bridge.call<PairingRemoveInfo>('pairingRemoveDaemon', {
      daemonId,
      url: url ?? undefined,
    });
    try {
      await bridge.call('refreshDaemons', { daemonId });
    } catch (err) {
      console.warn('[agt-settings] refreshDeferred after remove:', err);
    }
    daemonRemove.notice = tr('settings.pairingRemoved', { name: res.name || daemonId });
    void call('refresh');
  } catch (err) {
    daemonRemove.error = pairingErrorText(err);
  } finally {
    daemonRemove.busyId = null;
    render();
  }
}

/**
 * "Add daemon" modal: opened by the daemons card's button, hosts the
 * invite-code flow (form → generated command). Replaces the old separate
 * "Connection & pairing" card and the legacy file-append form.
 */
function renderAddDaemonModal(): string {
  if (!pairing.addOpen || !pairingEnabled()) {
    return '';
  }
  const invite = pairing.result;
  const copied = Date.now() - pairingCopiedAt < 1600;

  let body: string;
  if (invite) {
    body = `
      <p class="agd-form-ok">${tr('settings.pairingAdded', { path: esc(pairing.daemonsPath) })}</p>
      <p class="agd-note">${tr('settings.pairingRunOnDaemon')}</p>
      <pre class="agd-cmd"><code>${esc(invite.command)}</code></pre>
      <p class="agd-note agd-dim">${tr('settings.pairingWarning')}</p>
      ${pairing.error ? `<p class="agd-form-err">${esc(pairing.error)}</p>` : ''}
      <div class="agd-actions agd-modal-actions">
        <button type="button" id="agd-pairing-copy-cmd" class="agd-btn">${copied ? tr('settings.copied') : tr('settings.copy')}</button>
        <span class="agd-flex"></span>
        ${invite.restartRequired
          ? `<button type="button" id="agd-pairing-connect" class="agd-btn" ${invite.restarting ? 'disabled' : ''}>${invite.restarting ? spinner() + tr('settings.pairingConnecting') : tr('settings.pairingConnect')}</button>`
          : ''}
        <button type="button" id="agd-pairing-done" class="agd-btn agd-btn-primary" ${invite.restarting ? 'disabled' : ''}>${tr('settings.pairingDone')}</button>
      </div>`;
  } else {
    body = `
      <p class="agd-note">${tr('settings.pairingAddTitle')}</p>
      <label class="agd-field">${tr('settings.daemonName')}<input id="agd-pairing-name" class="agd-input" value="${esc(pairing.draft.name)}" autocomplete="off"></label>
      <label class="agd-field">${tr('settings.daemonUrl')}<input id="agd-pairing-host" class="agd-input agd-mono" placeholder="1.2.3.4" value="${esc(pairing.draft.host)}" autocomplete="off"><span id="agd-pairing-host-hint" class="agd-daemon-config-meta">${daemonUrl(pairing.draft.host) ? tr('settings.daemonHostHint', { url: daemonUrl(pairing.draft.host) }) : ''}</span></label>
      <label class="agd-field">${tr('settings.daemonCaps')}<input id="agd-pairing-caps" class="agd-input" placeholder="${tr('settings.daemonCapsPh')}" value="${esc(pairing.draft.capabilities)}" autocomplete="off"></label>
      ${pairing.error ? `<p class="agd-form-err">${esc(pairing.error)}</p>` : ''}
      <div class="agd-actions agd-modal-actions">
        <span class="agd-flex"></span>
        <button type="button" id="agd-pairing-cancel" class="agd-btn" ${pairing.busy ? 'disabled' : ''}>${tr('settings.cancel')}</button>
        <button type="button" id="agd-pairing-submit" class="agd-btn agd-btn-primary" ${pairing.busy ? 'disabled' : ''}>${pairing.busy ? spinner() : ''}${tr('settings.pairingAddSubmit')}</button>
      </div>`;
  }

  return `
  <div class="agd-modal-backdrop" id="agd-add-daemon-backdrop">
    <section class="agd-modal" role="dialog" aria-modal="true" aria-labelledby="agd-add-daemon-title">
      <header class="agd-modal-head">
        <div>
          <h3 id="agd-add-daemon-title">${tr('settings.addDaemon')}</h3>
        </div>
        <button type="button" id="agd-add-daemon-close" class="agd-btn" ${pairing.busy || invite?.restarting ? 'disabled' : ''}>${tr('settings.daemonConfigClose')}</button>
      </header>
      ${body}
    </section>
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
        `<span class="agd-chip">${providerLogo(p.provider)}${esc(p.provider)}</span>`,
    )
    .join('');
}

function identityStatusPill(status: string): string {
  const cls = status === 'active'
    ? 'agd-pill-ok'
    : status === 'revoked'
      ? 'agd-pill-err'
      : 'agd-pill-warn';
  return `<span class="agd-pill ${cls}">${esc(status)}</span>`;
}

function identityByDid(did: string | null): IdentityWithBindings | undefined {
  return did ? boot?.identities.find((identity) => identity.did === did) : undefined;
}

function firstBindingDaemon(): DaemonInfo | undefined {
  return (
    boot?.daemons.find((daemon) => daemon.daemon_id === boot?.localDaemonId)
    ?? boot?.daemons.find((daemon) => daemon.online)
    ?? boot?.daemons[0]
  );
}

function resetBindingDraft(): void {
  const daemon = firstBindingDaemon();
  identityModal.binding = {
    daemonId: daemon?.daemon_id ?? '',
    provider: daemon?.providers[0]?.provider ?? '',
    priority: '0',
    isDefault: false,
  };
}

function openIdentityModal(
  mode: IdentityModalMode,
  identity?: IdentityWithBindings,
): void {
  identityModal = {
    open: true,
    mode,
    did: identity?.did ?? null,
    busy: false,
    error: null,
    draft: identity
      ? {
          name: identity.name,
          slug: identity.did.replace(/^did:agt:/, ''),
          description: identity.description,
          capabilities: identity.capabilities.join(', '),
        }
      : { name: '', slug: '', description: '', capabilities: '' },
    binding: identityModal.binding,
  };
  resetBindingDraft();
  render();
}

function parseCapabilities(value: string): string[] {
  return [...new Set(
    value
      .split(/[,\n，]/)
      .map((item) => item.trim())
      .filter(Boolean),
  )];
}

function renderIdentityForm(): string {
  return `
    <div class="agd-identity-form">
      <label class="agd-field">
        ${tr('settings.identityName')}
        <input id="agd-identity-name" class="agd-input" value="${esc(identityModal.draft.name)}" autocomplete="off">
      </label>
      <label class="agd-field">
        ${tr('settings.identitySlug')}
        <input id="agd-identity-slug" class="agd-input agd-mono" value="${esc(identityModal.draft.slug)}" placeholder="${tr('settings.identitySlugPh')}" ${identityModal.mode === 'edit' ? 'disabled' : ''}>
      </label>
      <label class="agd-field">
        ${tr('settings.identityDescription')}
        <textarea id="agd-identity-description" class="agd-input agd-identity-textarea" rows="3" placeholder="${tr('settings.identityDescriptionPh')}">${esc(identityModal.draft.description)}</textarea>
      </label>
      <label class="agd-field">
        ${tr('settings.identityCapabilities')}
        <input id="agd-identity-capabilities" class="agd-input" value="${esc(identityModal.draft.capabilities)}" placeholder="${tr('settings.identityCapabilitiesPh')}">
      </label>
      ${identityModal.error ? `<p class="agd-form-err">${esc(identityModal.error)}</p>` : ''}
    </div>`;
}

function renderIdentityBindings(identity: IdentityWithBindings): string {
  const bindings = [...identity.bindings].sort(
    (a, b) =>
      Number(b.is_default) - Number(a.is_default)
      || b.priority - a.priority
      || a.id - b.id,
  );
  const rows = bindings
    .map((binding) => {
      const runtime = binding.runtime_online === true
        ? `<span class="agd-pill agd-pill-ok">${tr('settings.identityRuntimeOnline')}</span>`
        : binding.runtime_online === false
          ? `<span class="agd-pill agd-pill-err">${tr('settings.identityRuntimeOffline')}</span>`
          : `<span class="agd-pill">${tr('settings.identityRuntimeUnknown')}</span>`;
      return `
        <div class="agd-identity-binding" data-binding-id="${binding.id}">
          <div class="agd-identity-binding-main">
            <span class="agd-identity-runtime">${providerLogo(binding.provider)}<span class="agd-mono">${esc(binding.daemon_id)}</span><strong>${esc(binding.provider)}</strong></span>
            <span class="agd-identity-binding-badges">
              ${binding.is_default ? `<span class="agd-pill agd-pill-ok">${tr('settings.identityDefault')}</span>` : ''}
              ${runtime}
            </span>
          </div>
          <div class="agd-identity-binding-controls">
            <label>${tr('settings.identityPriority')}<input class="agd-input agd-mono" type="number" step="1" data-binding-priority="${binding.id}" value="${binding.priority}"></label>
            ${binding.is_default ? '' : `<button type="button" class="agd-btn" data-binding-default="${binding.id}">${tr('settings.identitySetDefault')}</button>`}
            <button type="button" class="agd-btn agd-btn-danger" data-binding-remove="${binding.id}">${tr('settings.identityRemoveBinding')}</button>
          </div>
        </div>`;
    })
    .join('');

  const daemons = boot?.daemons ?? [];
  const selectedDaemon = daemons.find(
    (daemon) => daemon.daemon_id === identityModal.binding.daemonId,
  );
  const providers = selectedDaemon?.providers.map((provider) => provider.provider) ?? [];
  const daemonOptions = daemons
    .map(
      (daemon) => `
        <option value="${esc(daemon.daemon_id)}" ${daemon.daemon_id === identityModal.binding.daemonId ? 'selected' : ''}>
          ${esc(daemon.daemon_name || daemon.device_name || daemon.daemon_id)}
        </option>`,
    )
    .join('');
  const providerOptions = providers
    .map(
      (provider) => `
        <option value="${esc(provider)}" ${provider === identityModal.binding.provider ? 'selected' : ''}>${esc(provider)}</option>`,
    )
    .join('');

  return `
    <section class="agd-identity-section">
      <div class="agd-identity-section-head">
        <span>${tr('settings.identityBindings')}</span>
        <span class="agd-flex"></span>
        <small class="agd-dim">${tr('settings.identityBindingOptionalHint')}</small>
      </div>
      ${rows || `<p class="agd-identity-empty">${tr('settings.identityNoBindings')}</p>`}
      ${
        daemons.length === 0
          ? `<p class="agd-identity-empty">${tr('settings.identityNoDaemons')}</p>`
          : `
          <div class="agd-identity-binding-form">
            <label class="agd-field">${tr('settings.identityBindingDaemon')}<select id="agd-identity-binding-daemon" class="agd-input">${daemonOptions}</select></label>
            <label class="agd-field">${tr('settings.identityBindingProvider')}<select id="agd-identity-binding-provider" class="agd-input" ${providers.length === 0 ? 'disabled' : ''}>${providerOptions}</select></label>
            <label class="agd-field">${tr('settings.identityPriority')}<input id="agd-identity-binding-priority" class="agd-input agd-mono" type="number" step="1" value="${esc(identityModal.binding.priority)}"></label>
            <label class="agd-switch-row agd-identity-default-switch">
              <span class="agd-switch"><input id="agd-identity-binding-default" type="checkbox" ${identityModal.binding.isDefault ? 'checked' : ''}><i></i></span>
              <span>${tr('settings.identityDefault')}</span>
            </label>
            <button type="button" class="agd-btn agd-btn-primary" id="agd-identity-binding-add" ${providers.length === 0 ? 'disabled' : ''}>${tr('settings.identityAddBinding')}</button>
          </div>
          ${providers.length === 0 ? `<p class="agd-form-err">${tr('settings.identityNoProvider')}</p>` : ''}`
      }
      ${identityModal.error ? `<p class="agd-form-err">${esc(identityModal.error)}</p>` : ''}
    </section>`;
}

function renderIdentityModal(): string {
  if (!identityModal.open) return '';
  const identity = identityByDid(identityModal.did);
  const isCreate = identityModal.mode === 'create';
  const isView = identityModal.mode === 'view';
  const title = isCreate
    ? tr('settings.identityCreateTitle')
    : identityModal.mode === 'edit'
      ? tr('settings.identityEditTitle')
      : identity?.name ?? tr('settings.identityDetails');
  const subtitle = isCreate
    ? tr('settings.identityCreateHint')
    : identity?.did ?? '';

  let body = '';
  if (!isCreate && !identity) {
    body = `<p class="agd-form-err">${tr('settings.identityNotFound')}</p>`;
  } else if (isView && identity) {
    body = `
      <div class="agd-identity-summary">
        <div class="agd-identity-summary-head">
          <strong>${esc(identity.name)}</strong>
          ${identityStatusPill(identity.status)}
        </div>
        <p>${esc(identity.description || tr('settings.identityNoDescription'))}</p>
        <div class="agd-chips">
          ${
            identity.capabilities.length > 0
              ? identity.capabilities.map((capability) => `<span class="agd-chip">${esc(capability)}</span>`).join('')
              : `<span class="agd-dim">${tr('settings.identityNoCapabilities')}</span>`
          }
        </div>
        <dl class="agd-identity-meta">
          <div><dt>${tr('settings.identityOwner')}</dt><dd class="agd-mono">${esc(identity.daemon_id)}</dd></div>
          <div><dt>${tr('settings.identityVisibility')}</dt><dd>${esc(identity.visibility)}</dd></div>
          <div><dt>${tr('settings.identityUpdated')}</dt><dd>${esc(identity.updated_at)}</dd></div>
        </dl>
      </div>
      ${renderIdentityBindings(identity)}`;
  } else {
    body = renderIdentityForm();
  }

  const footer = isView
    ? `
      <button type="button" class="agd-btn" id="agd-identity-close" ${identityModal.busy ? 'disabled' : ''}>${tr('settings.identityClose')}</button>
      <span class="agd-flex"></span>
      ${identity ? `<button type="button" class="agd-btn agd-btn-primary" id="agd-identity-edit">${tr('settings.identityEdit')}</button>` : ''}`
    : `
      <button type="button" class="agd-btn" id="agd-identity-cancel" ${identityModal.busy ? 'disabled' : ''}>${tr('settings.cancel')}</button>
      <span class="agd-flex"></span>
      <button type="button" class="agd-btn agd-btn-primary" id="agd-identity-save" ${identityModal.busy ? 'disabled' : ''}>
        ${identityModal.busy ? spinner() : ''}${isCreate ? tr('settings.identityCreate') : tr('settings.identitySave')}
      </button>`;

  return `
  <div class="agd-modal-backdrop" id="agd-identity-backdrop">
    <section class="agd-modal agd-identity-modal" role="dialog" aria-modal="true" aria-labelledby="agd-identity-modal-title">
      <header class="agd-modal-head">
        <div>
          <h3 id="agd-identity-modal-title">${esc(title)}</h3>
          <p class="agd-dim agd-mono">${esc(subtitle)}</p>
        </div>
        ${isView ? '' : `<button type="button" class="agd-btn" id="agd-identity-form-close" ${identityModal.busy ? 'disabled' : ''}>×</button>`}
      </header>
      ${body}
      <div class="agd-actions agd-modal-actions">${footer}</div>
    </section>
  </div>`;
}

function renderIdentitiesCard(): string {
  const identities = boot?.identities ?? [];
  const rows = identities
    .map(
      (identity) => `
      <tr class="is-clickable" data-identity-open="${esc(identity.did)}">
        <td data-label="${tr('settings.identity')}">
          <strong>${esc(identity.name)}</strong>
          <div class="agd-identity-did agd-mono">${esc(identity.did)}</div>
        </td>
        <td data-label="${tr('settings.status')}">${identityStatusPill(identity.status)}</td>
        <td data-label="${tr('settings.identityCapabilities')}"><span class="agd-chips">${
          identity.capabilities.length > 0
            ? identity.capabilities.slice(0, 4).map((capability) => `<span class="agd-chip">${esc(capability)}</span>`).join('')
            : `<span class="agd-dim">—</span>`
        }</span></td>
        <td data-label="${tr('settings.identityBindings')}">
          <span class="agd-identity-binding-count">${identity.bindings.length}</span>
          ${
            identity.bindings.length > 0
              ? `<span class="agd-dim"> ${tr('settings.identityBindingCount')}</span>`
              : `<span class="agd-dim"> ${tr('settings.identityNoBindingsShort')}</span>`
          }
        </td>
        <td class="agd-table-action"><button type="button" class="agd-btn agd-identity-view" data-identity-open="${esc(identity.did)}">${tr('settings.identityView')}</button></td>
      </tr>`,
    )
    .join('');

  return `
    <section class="agd-card">
      <div class="agd-card-head">
        <span>${tr('settings.identities')}</span>
        <span class="agd-flex"></span>
        <button type="button" class="agd-btn" id="agd-add-identity" ${boot?.state !== 'online' ? 'disabled' : ''}>${tr('settings.addIdentity')}</button>
      </div>
      <table class="agd-table agd-identity-table">
        <thead>
          <tr><th>${tr('settings.identity')}</th><th>${tr('settings.status')}</th><th>${tr('settings.identityCapabilities')}</th><th>${tr('settings.identityBindings')}</th><th></th></tr>
        </thead>
        <tbody>${rows || `<tr><td colspan="5"><div class="agd-empty-state agd-identity-empty-state"><div class="agd-empty-state-icon">◎</div><h3>${tr('settings.identityEmpty')}</h3><p>${tr('settings.identityEmptyHint')}</p></div></td></tr>`}</tbody>
      </table>
    </section>`;
}

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  if (!boot) {
    root.innerHTML = `<div class="agd-loading">…</div>`;
    return;
  }
  const currentBoot = boot;
  const lifecycleControl = currentBoot.capabilities?.lifecycleControl !== false;
  const daemonRows = currentBoot.daemons
    .map(
      (d) => `
      <tr class="${d.online ? 'is-clickable' : ''}" data-config-daemon-id="${esc(d.daemon_id)}">
        <td><span class="agd-mono">${esc(d.daemon_id)}</span></td>
        <td>${esc(d.device_name)}</td>
        <td><span class="agd-chips">${providerChips(d)}</span></td>
        <td><span class="agd-pill ${d.online ? 'agd-pill-ok' : 'agd-pill-err'}">${d.online ? 'online' : 'offline'}</span></td>
        <td class="agd-dim">${esc(d.last_seen)}</td>
        <td class="agd-table-action agd-table-actions">
          <button
            type="button"
            class="agd-btn agd-daemon-refresh-button"
            data-refresh-daemon-id="${esc(d.daemon_id)}"
            ${busyAction ? 'disabled' : ''}
            title="${tr('settings.daemonRefresh')}"
          >${busyAction === 'refreshDaemons' ? spinner() : '⟳'}</button>
          ${removableDaemon(d) ? `
          <button
            type="button"
            class="agd-btn agd-btn-danger agd-daemon-remove-button"
            data-remove-daemon-id="${esc(d.daemon_id)}"
            data-remove-daemon-url="${esc(d.url ?? '')}"
            ${busyAction || daemonRemove.busyId ? 'disabled' : ''}
            title="${tr('settings.pairingRemoveHint')}"
          >${daemonRemove.busyId === d.daemon_id
            ? spinner()
            : daemonRemove.confirmId === d.daemon_id
              ? tr('settings.pairingRemoveConfirm')
              : tr('settings.pairingRemove')}</button>` : ''}
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
        <input id="agd-url" class="agd-input agd-mono" value="${esc(boot.serverUrl)}" ${lifecycleControl ? '' : 'readonly'}>
        <button type="button" id="agd-test" class="agd-btn" ${busyAction ? 'disabled' : ''}>${busyAction === 'testConnection' ? spinner() : ''}${tr('settings.test')}</button>
      </label>
      ${lifecycleControl ? `
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
      </div>` : `<p class="agd-note agd-dim">${tr('settings.consoleLifecycleHint')}</p>`}
    </section>

    ${renderIdentitiesCard()}

    <section class="agd-card">
      <div class="agd-card-head">
        <span>${tr('settings.daemons')}</span>
        <span class="agd-flex"></span>
        <button type="button" id="agd-add-daemon" class="agd-btn" ${pairingEnabled() ? '' : 'disabled'}>${tr('settings.addDaemon')}</button>
      </div>
      <table class="agd-table">
        <thead>
          <tr><th>ID</th><th>${tr('settings.host')}</th><th>${tr('settings.providers')}</th><th>${tr('settings.status')}</th><th>${tr('settings.lastSeen')}</th><th></th></tr>
        </thead>
        <tbody>${daemonRows || `<tr><td colspan="6" class="agd-dim">${tr('tree.noDaemons')}</td></tr>`}</tbody>
      </table>
      ${daemonRemove.error ? `<p class="agd-form-err">${esc(daemonRemove.error)}</p>` : ''}
      ${daemonRemove.notice ? `<p class="agd-form-ok">${esc(daemonRemove.notice)}</p>` : ''}
      <p class="agd-note agd-dim">${tr('settings.daemonHint', { command: `agt daemon start --server ${boot.serverUrl}` })}</p>
      <p class="agd-note agd-dim">${tr('settings.daemonConfigLocalOnly')}</p>
    </section>

    ${bridge.isMock ? `<p class="agd-mock-note">browser preview · mock data</p>` : ''}
  </div>
  ${renderAddDaemonModal()}
  ${renderDaemonConfigModal()}
  ${renderIdentityModal()}`;

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

function applyIdentityResult(result: {
  identities?: IdentityWithBindings[];
  created?: IdentityWithBindings;
  updated?: IdentityWithBindings;
}): void {
  if (boot && Array.isArray(result.identities)) {
    boot = { ...boot, identities: result.identities };
  }
}

function identityError(err: unknown): string {
  return String(err instanceof Error ? err.message : err)
    .replace(/^Error:\s*/, '')
    .replace(/^HTTP \d+:\s*/, '');
}

async function saveIdentity(): Promise<void> {
  if (!identityModal.open || identityModal.busy) return;
  if (!identityModal.draft.name.trim()) {
    identityModal.error = tr('settings.identityNameRequired');
    render();
    return;
  }

  identityModal.busy = true;
  identityModal.error = null;
  render();
  try {
    const capabilities = parseCapabilities(identityModal.draft.capabilities);
    if (identityModal.mode === 'create') {
      const result = await bridge.call<{
        identities: IdentityWithBindings[];
        created: IdentityWithBindings;
      }>('identityCreate', {
        name: identityModal.draft.name.trim(),
        slug: identityModal.draft.slug.trim() || null,
        description: identityModal.draft.description.trim(),
        capabilities,
      });
      applyIdentityResult(result);
      identityModal = {
        ...identityModal,
        mode: 'view',
        did: result.created.did,
        draft: {
          name: result.created.name,
          slug: result.created.did.replace(/^did:agt:/, ''),
          description: result.created.description,
          capabilities: result.created.capabilities.join(', '),
        },
      };
    } else if (identityModal.did) {
      const result = await bridge.call<{
        identities: IdentityWithBindings[];
        updated: IdentityWithBindings;
      }>('identityUpdate', {
        did: identityModal.did,
        name: identityModal.draft.name.trim(),
        description: identityModal.draft.description.trim(),
        capabilities,
      });
      applyIdentityResult(result);
      identityModal = {
        ...identityModal,
        mode: 'view',
        draft: {
          name: result.updated.name,
          slug: result.updated.did.replace(/^did:agt:/, ''),
          description: result.updated.description,
          capabilities: result.updated.capabilities.join(', '),
        },
      };
    }
  } catch (err) {
    identityModal.error = identityError(err);
  } finally {
    identityModal.busy = false;
    render();
  }
}

async function addIdentityBinding(): Promise<void> {
  const identity = identityByDid(identityModal.did);
  if (!identity || identityModal.busy) return;
  if (!identityModal.binding.daemonId || !identityModal.binding.provider) {
    identityModal.error = tr('settings.identityBindingRequired');
    render();
    return;
  }
  const priority = Number(identityModal.binding.priority);
  if (!Number.isInteger(priority)) {
    identityModal.error = tr('settings.identityPriorityInteger');
    render();
    return;
  }

  identityModal.busy = true;
  identityModal.error = null;
  render();
  try {
    const result = await bridge.call<{ identities: IdentityWithBindings[] }>(
      'identityBindingCreate',
      {
        did: identity.did,
        daemonId: identityModal.binding.daemonId,
        provider: identityModal.binding.provider,
        priority,
        isDefault: identityModal.binding.isDefault,
      },
    );
    applyIdentityResult(result);
    identityModal.binding = {
      ...identityModal.binding,
      priority: '0',
      isDefault: false,
    };
  } catch (err) {
    identityModal.error = identityError(err);
  } finally {
    identityModal.busy = false;
    render();
  }
}

async function updateIdentityBinding(
  bindingId: number,
  input: { priority?: number; isDefault?: boolean },
): Promise<void> {
  const identity = identityByDid(identityModal.did);
  if (!identity || identityModal.busy) return;
  if (input.priority !== undefined && !Number.isInteger(input.priority)) {
    identityModal.error = tr('settings.identityPriorityInteger');
    render();
    return;
  }
  identityModal.busy = true;
  identityModal.error = null;
  render();
  try {
    const result = await bridge.call<{ identities: IdentityWithBindings[] }>(
      'identityBindingUpdate',
      {
        did: identity.did,
        bindingId,
        ...input,
      },
    );
    applyIdentityResult(result);
  } catch (err) {
    identityModal.error = identityError(err);
  } finally {
    identityModal.busy = false;
    render();
  }
}

async function removeIdentityBinding(bindingId: number): Promise<void> {
  const identity = identityByDid(identityModal.did);
  if (!identity || identityModal.busy) return;
  identityModal.busy = true;
  identityModal.error = null;
  render();
  try {
    const result = await bridge.call<{ identities: IdentityWithBindings[] }>(
      'identityBindingDelete',
      { did: identity.did, bindingId },
    );
    applyIdentityResult(result);
  } catch (err) {
    identityModal.error = identityError(err);
  } finally {
    identityModal.busy = false;
    render();
  }
}

function applyBoot(next: BootInfo): void {
  boot = next;
  lang = next.language;
  if (pairingEnabled() && !pairing.probed && !pairing.loading) {
    void loadPairing();
  }
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
  // Per-row refresh: re-scan the daemon target file on the Server and force
  // an immediate reconnect for this daemon. Enabled even when it is offline —
  // that is exactly the case the button exists for.
  document.querySelectorAll<HTMLButtonElement>('button[data-refresh-daemon-id]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      void call('refreshDaemons', { daemonId: btn.dataset.refreshDaemonId ?? '' });
    });
  });
  // Per-row remove: first click arms, second click deletes. The armed state
  // reverts on its own after a few seconds so it cannot be "left loaded".
  document.querySelectorAll<HTMLButtonElement>('button[data-remove-daemon-id]').forEach((btn) => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const daemonId = btn.dataset.removeDaemonId ?? '';
      if (!daemonId || daemonRemove.busyId) return;
      if (daemonRemove.confirmId !== daemonId) {
        daemonRemove.confirmId = daemonId;
        daemonRemove.error = null;
        daemonRemove.notice = null;
        render();
        window.setTimeout(() => {
          if (daemonRemove.confirmId === daemonId) {
            daemonRemove.confirmId = null;
            render();
          }
        }, 4000);
        return;
      }
      daemonRemove.confirmId = null;
      void removeDaemonTarget(daemonId, btn.dataset.removeDaemonUrl || null);
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
  document.getElementById('agd-add-identity')?.addEventListener('click', () => {
    if (boot?.state === 'online') openIdentityModal('create');
  });
  document.querySelectorAll<HTMLElement>('tr[data-identity-open]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if ((event.target as Element).closest('button')) return;
      const identity = identityByDid(row.dataset.identityOpen ?? null);
      if (identity) openIdentityModal('view', identity);
    });
  });
  document.querySelectorAll<HTMLElement>('[data-identity-open] button').forEach((button) => {
    button.addEventListener('click', () => {
      const row = button.closest<HTMLElement>('[data-identity-open]');
      const identity = identityByDid(row?.dataset.identityOpen ?? null);
      if (identity) openIdentityModal('view', identity);
    });
  });
  document.getElementById('agd-identity-close')?.addEventListener('click', () => {
    identityModal.open = false;
    identityModal.error = null;
    render();
  });
  document.getElementById('agd-identity-form-close')?.addEventListener('click', () => {
    if (identityModal.busy) return;
    if (identityModal.mode === 'create') {
      identityModal.open = false;
    } else {
      identityModal.mode = 'view';
    }
    identityModal.error = null;
    render();
  });
  document.getElementById('agd-identity-cancel')?.addEventListener('click', () => {
    if (identityModal.busy) return;
    if (identityModal.mode === 'create') {
      identityModal.open = false;
      identityModal.error = null;
      render();
      return;
    }
    identityModal.mode = 'view';
    identityModal.error = null;
    render();
  });
  document.getElementById('agd-identity-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !identityModal.busy) {
      identityModal.open = false;
      identityModal.error = null;
      render();
    }
  });
  document.getElementById('agd-identity-edit')?.addEventListener('click', () => {
    const identity = identityByDid(identityModal.did);
    if (identity) openIdentityModal('edit', identity);
  });
  document.getElementById('agd-identity-save')?.addEventListener('click', () => {
    void saveIdentity();
  });
  for (const [id, key] of [
    ['agd-identity-name', 'name'],
    ['agd-identity-slug', 'slug'],
    ['agd-identity-description', 'description'],
    ['agd-identity-capabilities', 'capabilities'],
  ] as const) {
    document.getElementById(id)?.addEventListener('input', (event) => {
      identityModal.draft[key] = (event.target as HTMLInputElement | HTMLTextAreaElement).value;
      identityModal.error = null;
    });
  }
  document.getElementById('agd-identity-binding-daemon')?.addEventListener('change', (event) => {
    const daemonId = (event.target as HTMLSelectElement).value;
    const daemon = boot?.daemons.find((item) => item.daemon_id === daemonId);
    identityModal.binding = {
      ...identityModal.binding,
      daemonId,
      provider: daemon?.providers[0]?.provider ?? '',
    };
    identityModal.error = null;
    render();
  });
  document.getElementById('agd-identity-binding-provider')?.addEventListener('change', (event) => {
    identityModal.binding.provider = (event.target as HTMLSelectElement).value;
    identityModal.error = null;
  });
  document.getElementById('agd-identity-binding-priority')?.addEventListener('input', (event) => {
    identityModal.binding.priority = (event.target as HTMLInputElement).value;
    identityModal.error = null;
  });
  document.getElementById('agd-identity-binding-default')?.addEventListener('change', (event) => {
    identityModal.binding.isDefault = (event.target as HTMLInputElement).checked;
    identityModal.error = null;
  });
  document.getElementById('agd-identity-binding-add')?.addEventListener('click', () => {
    void addIdentityBinding();
  });
  document.querySelectorAll<HTMLInputElement>('[data-binding-priority]').forEach((input) => {
    input.addEventListener('change', () => {
      void updateIdentityBinding(Number(input.dataset.bindingPriority), {
        priority: Number(input.value),
      });
    });
  });
  document.querySelectorAll<HTMLElement>('[data-binding-default]').forEach((button) => {
    button.addEventListener('click', () => {
      void updateIdentityBinding(Number(button.dataset.bindingDefault), {
        isDefault: true,
      });
    });
  });
  document.querySelectorAll<HTMLElement>('[data-binding-remove]').forEach((button) => {
    button.addEventListener('click', () => {
      void removeIdentityBinding(Number(button.dataset.bindingRemove));
    });
  });
  const closeAddDaemon = (): void => {
    pairing.addOpen = false;
    pairing.error = null;
    pairing.result = null;
    render();
  };
  document.getElementById('agd-add-daemon')?.addEventListener('click', () => {
    pairing.addOpen = true;
    pairing.error = null;
    pairing.result = null;
    render();
    document.getElementById('agd-pairing-name')?.focus();
  });
  document.getElementById('agd-add-daemon-close')?.addEventListener('click', closeAddDaemon);
  document.getElementById('agd-add-daemon-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !pairing.busy) closeAddDaemon();
  });
  document.getElementById('agd-pairing-cancel')?.addEventListener('click', closeAddDaemon);
  for (const [id, key] of [
    ['agd-pairing-name', 'name'],
    ['agd-pairing-host', 'host'],
    ['agd-pairing-caps', 'capabilities'],
  ] as const) {
    document.getElementById(id)?.addEventListener('input', (e) => {
      const value = (e.target as HTMLInputElement).value;
      pairing.draft[key] = value;
      if (key === 'host') syncDaemonHostHint(id, value);
    });
  }
  document.getElementById('agd-pairing-submit')?.addEventListener('click', () => {
    void submitPairingForm();
  });
  document.getElementById('agd-pairing-copy-cmd')?.addEventListener('click', async () => {
    const command = pairing.result?.command;
    if (!command) {
      return;
    }
    await copyText(command);
    pairingCopiedAt = Date.now();
    render();
  });
  document.getElementById('agd-pairing-connect')?.addEventListener('click', () => {
    void connectPairingTarget();
  });
  document.getElementById('agd-pairing-done')?.addEventListener('click', () => {
    closeAddDaemon();
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
