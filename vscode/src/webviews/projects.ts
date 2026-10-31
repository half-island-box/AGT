import './mode-switcher.css';
import './styles.css';
import './projects.css';
import { t, type MessageKey } from '../core/i18n';
import type {
  ConsoleProjectInfo,
  ServerState,
  UiLanguage,
  UsageSummary,
} from '../core/types';
import { bridge } from './bridge';
import { openDirPicker } from './dir-picker';

interface RemoteDaemonOption {
  id: string;
  label: string;
  online: boolean;
}

interface ProjectsBoot {
  language: UiLanguage;
  state: ServerState;
  projects: ConsoleProjectInfo[];
  remoteDaemons: RemoteDaemonOption[];
}

interface ProjectModal {
  kind: 'create' | 'rename' | 'delete';
  busy: boolean;
  error: string | null;
  projectId: string | null;
  name: string;
  path: string;
  pathInput: string;
  pickerBusy: boolean;
  remoteDaemonId: string;
  remotePath: string;
  remotePathInput: string;
}

let boot: ProjectsBoot | null = null;
let lang: UiLanguage = 'zh';
let modal: ProjectModal | null = null;
let openingProjectId: string | null = null;

type UsageRange = 'today' | '7d' | '30d' | '90d' | 'all';

interface DashboardState {
  agent: string; // 'all' or a provider key
  range: UsageRange;
  loading: boolean;
  error: string | null;
  data: UsageSummary | null;
}

const AGENT_LABELS: Record<string, string> = {
  claude: 'Claude Code',
  codex: 'Codex',
  'cursor-agent': 'Cursor',
  copilot: 'Copilot',
  qwen: 'Qwen',
};
const AGENT_KEYS = Object.keys(AGENT_LABELS);

let dashboard: DashboardState = {
  agent: 'all',
  range: '30d',
  loading: false,
  error: null,
  data: null,
};

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

function cleanError(err: unknown): string {
  return String(err instanceof Error ? err.message : err)
    .replace(/^Error:\s*/, '')
    .replace(/^HTTP \d+:\s*/, '');
}

function spinner(): string {
  return '<span class="agd-spinner" aria-hidden="true"></span>';
}

function counts(project: ConsoleProjectInfo): string {
  return tr('projects.counts', {
    chat: project.run_counts.chat,
    sop: project.run_counts.sop,
    goal: project.run_counts.goal,
  });
}

function formatDate(value: string | null): string {
  if (!value) return tr('projects.neverOpened');
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function remoteDaemonLabel(daemonId: string | null): string {
  if (!daemonId) return '';
  return boot?.remoteDaemons.find((daemon) => daemon.id === daemonId)?.label ?? daemonId;
}

function renderProject(project: ConsoleProjectInfo): string {
  const busy = openingProjectId === project.id;
  const remoteLine = project.remote && project.remote_path
    ? `<div class="agd-project-path agd-mono" title="${esc(project.remote_path)}">↗ ${esc(remoteDaemonLabel(project.remote_daemon_id))} · ${esc(project.remote_path)}</div>`
    : '';
  return `
    <article class="agd-project-card ${project.available ? '' : 'is-unavailable'}" data-project-open="${esc(project.id)}">
      <div class="agd-project-icon">⌂</div>
      <div class="agd-project-main">
        <div class="agd-project-title-line">
          <strong>${esc(project.name)}</strong>
          <span class="agd-pill ${project.available ? 'agd-pill-ok' : 'agd-pill-err'}">${project.available ? tr('projects.available') : tr('projects.unavailable')}</span>
          ${project.remote ? `<span class="agd-pill">${tr('projects.remote')}</span>` : ''}
        </div>
        <div class="agd-project-path agd-mono" title="${esc(project.path)}">${esc(project.path)}</div>
        ${remoteLine}
        <div class="agd-project-meta">
          <span>${counts(project)}</span>
          <span>${tr('projects.lastOpened')}: ${esc(formatDate(project.last_opened_at))}</span>
        </div>
      </div>
      <div class="agd-project-actions">
        <button type="button" class="agd-btn agd-btn-primary" data-project-open-button="${esc(project.id)}" ${!project.available || busy ? 'disabled' : ''}>${busy ? '…' : tr('projects.open')}</button>
        <button type="button" class="agd-btn" data-project-rename="${esc(project.id)}">${tr('projects.rename')}</button>
        <button type="button" class="agd-btn agd-btn-danger" data-project-delete="${esc(project.id)}">${tr('projects.remove')}</button>
      </div>
    </article>`;
}

function renderProjects(): string {
  const projects = boot?.projects ?? [];
  if (projects.length === 0) {
    return `
      <div class="agd-card agd-projects-empty">
        <div class="agd-empty-state">
          <div class="agd-empty-state-icon">⌂</div>
          <h3>${tr('projects.empty')}</h3>
          <p>${tr('projects.emptyHint')}</p>
          <button type="button" class="agd-btn agd-btn-primary" id="agd-project-add-empty">${tr('projects.add')}</button>
        </div>
      </div>`;
  }
  return `<div class="agd-project-list">${projects.map(renderProject).join('')}</div>`;
}

function fmtNum(value: number): string {
  return value.toLocaleString();
}

function fmtTokens(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}K`;
  return String(value);
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

function fmtTs(d: Date): string {
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} `
    + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
}

function usageWindow(range: UsageRange): {
  from: string;
  to: string;
  bucket: 'hour' | 'day' | 'month';
} {
  const to = new Date();
  let from = new Date(to);
  let bucket: 'hour' | 'day' | 'month' = 'day';
  switch (range) {
    case 'today':
      from.setUTCHours(0, 0, 0, 0);
      bucket = 'hour';
      break;
    case '7d':
      from.setUTCDate(from.getUTCDate() - 7);
      break;
    case '30d':
      from.setUTCDate(from.getUTCDate() - 30);
      break;
    case '90d':
      from.setUTCDate(from.getUTCDate() - 90);
      break;
    case 'all':
      from = new Date(0);
      bucket = 'month';
      break;
  }
  return { from: fmtTs(from), to: fmtTs(to), bucket };
}

function agentLabel(agent: string): string {
  return AGENT_LABELS[agent] ?? agent;
}

function renderStatCard(label: string, value: string, icon: string): string {
  return `
    <div class="agd-usage-stat">
      <div class="agd-usage-stat-icon">${icon}</div>
      <div class="agd-usage-stat-body">
        <span class="agd-usage-stat-label">${esc(label)}</span>
        <strong class="agd-usage-stat-value">${esc(value)}</strong>
      </div>
    </div>`;
}

function renderUsageChart(series: UsageSummary['series']): string {
  if (!series.length) return '';
  const W = 640;
  const H = 180;
  const padX = 6;
  const padTop = 10;
  const padBottom = 22;
  const innerW = W - padX * 2;
  const innerH = H - padTop - padBottom;
  const max = Math.max(1, ...series.map((p) => p.input_tokens));
  const step = innerW / series.length;
  const barW = Math.max(1.5, step * 0.6);
  const bars = series.map((p, i) => {
    const h = (p.input_tokens / max) * innerH;
    const x = padX + i * step + (step - barW) / 2;
    const y = padTop + innerH - h;
    return `<rect class="agd-usage-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h, 0.5).toFixed(1)}" rx="1.5"><title>${esc(p.bucket)} · ${fmtNum(p.input_tokens)} tokens</title></rect>`;
  }).join('');
  return `
    <svg class="agd-usage-chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(tr('projects.usage.trend'))}">
      <line class="agd-usage-grid" x1="${padX}" y1="${padTop}" x2="${W - padX}" y2="${padTop}"/>
      <line class="agd-usage-grid" x1="${padX}" y1="${padTop + innerH / 2}" x2="${W - padX}" y2="${padTop + innerH / 2}"/>
      <line class="agd-usage-grid" x1="${padX}" y1="${padTop + innerH}" x2="${W - padX}" y2="${padTop + innerH}"/>
      <text class="agd-usage-axis" x="${padX}" y="${padTop - 3}">${fmtTokens(max)}</text>
      <text class="agd-usage-axis" x="${padX}" y="${H - 7}">${esc(series[0].bucket)}</text>
      <text class="agd-usage-axis" x="${W - padX}" y="${H - 7}" text-anchor="end">${esc(series[series.length - 1].bucket)}</text>
      ${bars}
    </svg>`;
}

function renderBreakdown(items: UsageSummary['breakdown']): string {
  if (!items.length) return '';
  const rows = items.map((item) => `
    <tr>
      <td class="agd-mono">${esc(item.daemon_id ?? tr('projects.usage.unknownDaemon'))}</td>
      <td>${esc(agentLabel(item.agent))}</td>
      <td class="agd-mono">${esc(item.model ?? '—')}</td>
      <td class="agd-num">${fmtNum(item.input_tokens)}</td>
      <td class="agd-num">${fmtNum(item.output_tokens)}</td>
      <td class="agd-num">${fmtNum(item.cache_read_input_tokens)}</td>
      <td class="agd-num">${fmtNum(item.steps)}</td>
    </tr>`).join('');
  return `
    <div class="agd-usage-table-wrap">
      <table class="agd-usage-table">
        <thead>
          <tr>
            <th>${esc(tr('projects.usage.daemon'))}</th>
            <th>${esc(tr('projects.usage.agent'))}</th>
            <th>${esc(tr('projects.usage.model'))}</th>
            <th class="agd-num">${esc(tr('projects.usage.input'))}</th>
            <th class="agd-num">${esc(tr('projects.usage.output'))}</th>
            <th class="agd-num">${esc(tr('projects.usage.cacheRead'))}</th>
            <th class="agd-num">${esc(tr('projects.usage.steps'))}</th>
          </tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`;
}

function renderDashboard(): string {
  const agentOptions = [tr('projects.usage.agentAll'), ...AGENT_KEYS.map(agentLabel)]
    .map((label, i) => {
      const value = i === 0 ? 'all' : AGENT_KEYS[i - 1];
      const selected = dashboard.agent === value ? ' selected' : '';
      return `<option value="${esc(value)}"${selected}>${esc(label)}</option>`;
    }).join('');

  const ranges: Array<{ value: UsageRange; label: string }> = [
    { value: 'today', label: tr('projects.usage.rangeToday') },
    { value: '7d', label: tr('projects.usage.range7d') },
    { value: '30d', label: tr('projects.usage.range30d') },
    { value: '90d', label: tr('projects.usage.range90d') },
    { value: 'all', label: tr('projects.usage.rangeAll') },
  ];
  const rangeOptions = ranges.map((r) => {
    const selected = dashboard.range === r.value ? ' selected' : '';
    return `<option value="${r.value}"${selected}>${esc(r.label)}</option>`;
  }).join('');

  let body: string;
  if (dashboard.loading) {
    body = `<div class="agd-usage-empty">${spinner()} ${esc(tr('projects.usage.loading'))}</div>`;
  } else if (dashboard.error) {
    body = `<div class="agd-usage-empty agd-usage-error">${esc(dashboard.error)}</div>`;
  } else if (!dashboard.data || dashboard.data.totals.steps === 0) {
    body = `<div class="agd-usage-empty">${esc(tr('projects.usage.empty'))}</div>`;
  } else {
    const totals = dashboard.data.totals;
    const cards = [
      renderStatCard(tr('projects.usage.input'), fmtNum(totals.input_tokens), '↓'),
      renderStatCard(tr('projects.usage.output'), fmtNum(totals.output_tokens), '↑'),
      renderStatCard(tr('projects.usage.cacheRead'), fmtNum(totals.cache_read_input_tokens), '↻'),
      renderStatCard(tr('projects.usage.steps'), fmtNum(totals.steps), '#'),
    ].join('');
    body = `
      <div class="agd-usage-cards">${cards}</div>
      <div class="agd-usage-chart-wrap">
        <h4>${esc(tr('projects.usage.trend'))}</h4>
        ${renderUsageChart(dashboard.data.series)}
      </div>
      <div class="agd-usage-breakdown">
        <h4>${esc(tr('projects.usage.breakdown'))}</h4>
        ${renderBreakdown(dashboard.data.breakdown)}
      </div>`;
  }

  return `
    <section class="agd-usage-dashboard">
      <header class="agd-usage-head">
        <h2>${esc(tr('projects.usage.title'))}</h2>
        <div class="agd-usage-filters">
          <label class="agd-usage-filter">
            <span>${esc(tr('projects.usage.agent'))}</span>
            <select id="agd-usage-agent" class="agd-input">${agentOptions}</select>
          </label>
          <label class="agd-usage-filter">
            <span>${esc(tr('projects.usage.range'))}</span>
            <select id="agd-usage-range" class="agd-input">${rangeOptions}</select>
          </label>
        </div>
      </header>
      ${body}
    </section>`;
}

async function loadUsage(): Promise<void> {
  if (bridge.isMock) return;
  dashboard.loading = true;
  dashboard.error = null;
  render();
  const { from, to, bucket } = usageWindow(dashboard.range);
  try {
    const data = await bridge.call<UsageSummary>('projects.usage', {
      agent: dashboard.agent,
      from,
      to,
      bucket,
    });
    dashboard.data = data;
  } catch (err) {
    dashboard.error = cleanError(err);
  } finally {
    dashboard.loading = false;
    render();
  }
}

function renderRemoteSection(modal: ProjectModal): string {
  const daemons = boot?.remoteDaemons ?? [];
  const options = [`<option value="">${tr('projects.remoteNone')}</option>`]
    .concat(daemons.map((daemon) => `<option value="${esc(daemon.id)}" ${modal.remoteDaemonId === daemon.id ? 'selected' : ''}>${esc(daemon.label)}${daemon.online ? '' : ` · ${tr('status.offline')}`}</option>`))
    .join('');
  const pathRow = modal.remoteDaemonId
    ? `<div class="agd-project-path-input">
          <input id="agd-project-remote-path" class="agd-input agd-mono" value="${esc(modal.remotePathInput || modal.remotePath)}" placeholder="/absolute/path">
          <button type="button" class="agd-btn" id="agd-project-browse-remote">${tr('projects.browse')}</button>
        </div>`
    : '';
  return `
    <div class="agd-project-remote">
      <div class="agd-project-remote-head">
        <strong>${tr('projects.remoteSection')}</strong>
        <small class="agd-dim">${tr('projects.remoteHint')}</small>
      </div>
      <label class="agd-field">${tr('projects.remoteDaemon')}<select id="agd-project-remote-daemon" class="agd-input">${options}</select></label>
      ${pathRow}
    </div>`;
}

function renderModal(): string {
  if (!modal) return '';
  const project = boot?.projects.find((item) => item.id === modal?.projectId);
  if (modal.kind === 'create') {
    return `
    <div class="agd-modal-backdrop" id="agd-project-modal-backdrop">
      <section class="agd-modal agd-project-modal" role="dialog" aria-modal="true">
        <header class="agd-modal-head">
          <div><h3>${tr('projects.addTitle')}</h3><p>${tr('projects.addHint')}</p></div>
          <button type="button" class="agd-btn" id="agd-project-modal-close" ${modal.busy ? 'disabled' : ''}>×</button>
        </header>
        <label class="agd-field">${tr('projects.name')}<input id="agd-project-name" class="agd-input" value="${esc(modal.name)}" placeholder="${tr('projects.namePh')}"></label>
        <div class="agd-project-path-input">
          <input id="agd-project-path" class="agd-input agd-mono" value="${esc(modal.pathInput || modal.path)}" placeholder="/absolute/path">
          <button type="button" class="agd-btn" id="agd-project-browse-path" ${modal.pickerBusy ? 'disabled' : ''}>${modal.pickerBusy ? `${spinner()}${tr('projects.picking')}` : tr('projects.browse')}</button>
        </div>
        ${renderRemoteSection(modal)}
        ${modal.error ? `<p class="agd-form-err">${esc(modal.error)}</p>` : ''}
        <div class="agd-actions agd-modal-actions">
          <button type="button" class="agd-btn" id="agd-project-cancel" ${modal.busy ? 'disabled' : ''}>${tr('settings.cancel')}</button>
          <span class="agd-flex"></span>
          <button type="button" class="agd-btn agd-btn-primary" id="agd-project-create" ${modal.busy || !(modal.pathInput || modal.path) || Boolean(modal.remoteDaemonId && !(modal.remotePathInput || modal.remotePath)) ? 'disabled' : ''}>${modal.busy ? tr('projects.creating') : tr('projects.create')}</button>
        </div>
      </section>
    </div>`;
  }
  if (modal.kind === 'rename') {
    return `
    <div class="agd-modal-backdrop" id="agd-project-modal-backdrop">
      <section class="agd-modal agd-project-modal" role="dialog" aria-modal="true">
        <header class="agd-modal-head"><div><h3>${tr('projects.renameTitle')}</h3><p class="agd-mono">${esc(project?.path ?? '')}</p></div><button type="button" class="agd-btn" id="agd-project-modal-close">×</button></header>
        <label class="agd-field">${tr('projects.name')}<input id="agd-project-rename-name" class="agd-input" value="${esc(modal.name)}"></label>
        ${modal.error ? `<p class="agd-form-err">${esc(modal.error)}</p>` : ''}
        <div class="agd-actions agd-modal-actions"><button type="button" class="agd-btn" id="agd-project-cancel">${tr('settings.cancel')}</button><span class="agd-flex"></span><button type="button" class="agd-btn agd-btn-primary" id="agd-project-rename-save" ${modal.busy ? 'disabled' : ''}>${modal.busy ? '…' : tr('projects.save')}</button></div>
      </section>
    </div>`;
  }
  return `
    <div class="agd-modal-backdrop" id="agd-project-modal-backdrop">
      <section class="agd-modal agd-project-modal" role="dialog" aria-modal="true">
        <header class="agd-modal-head"><div><h3>${tr('projects.removeTitle')}</h3><p>${esc(project?.name ?? '')}</p></div><button type="button" class="agd-btn" id="agd-project-modal-close">×</button></header>
        <p class="agd-project-delete-warning">${tr('projects.removeWarning')}</p>
        <code class="agd-mono">${esc(project?.path ?? '')}</code>
        ${modal.error ? `<p class="agd-form-err">${esc(modal.error)}</p>` : ''}
        <div class="agd-actions agd-modal-actions"><button type="button" class="agd-btn" id="agd-project-cancel">${tr('settings.cancel')}</button><span class="agd-flex"></span><button type="button" class="agd-btn agd-btn-danger" id="agd-project-delete-confirm" ${modal.busy ? 'disabled' : ''}>${modal.busy ? '…' : tr('projects.removeConfirm')}</button></div>
      </section>
    </div>`;
}

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  if (!boot) {
    root.innerHTML = `<div class="agd-loading">…</div>`;
    return;
  }
  root.innerHTML = `
  <div class="agd-wrap agd-projects-page">
    <header class="agd-head">
      <div class="agd-head-main">
        <div class="agd-title">${tr('app.title')} <span class="agd-sub">${tr('projects.title')}</span></div>
      </div>
      <div class="agd-head-tools">
        <button type="button" class="agd-lang" id="agd-projects-settings" title="${tr('chat.openSettings')}">⚙</button>
        <button type="button" class="agd-lang" id="agd-projects-lang">${tr('settings.langToggle')}</button>
      </div>
    </header>
    <div class="agd-projects-hero">
      <div><h1>${tr('projects.heading')}</h1><p>${tr('projects.subtitle')}</p></div>
      <button type="button" class="agd-btn agd-btn-primary" id="agd-project-add">${tr('projects.add')}</button>
    </div>
    ${renderProjects()}
    ${renderDashboard()}
    ${bridge.isMock ? `<p class="agd-mock-note">browser preview · mock data</p>` : ''}
  </div>
  ${renderModal()}`;
  wireEvents();
}

async function refresh(): Promise<void> {
  boot = await bridge.call<ProjectsBoot>('projects.boot');
  lang = boot.language;
  render();
}

function openCreate(): void {
  modal = {
    kind: 'create',
    busy: false,
    error: null,
    projectId: null,
    name: '',
    path: '',
    pathInput: '',
    pickerBusy: false,
    remoteDaemonId: '',
    remotePath: '',
    remotePathInput: '',
  };
  render();
}

async function pickDirectory(): Promise<void> {
  if (!modal || modal.kind !== 'create' || modal.pickerBusy) return;
  modal.pickerBusy = true;
  modal.error = null;
  render();
  try {
    const result = await bridge.call<{ path: string | null }>(
      'projects.pickDirectory',
    );
    if (result.path && modal) {
      modal.path = result.path;
      modal.pathInput = result.path;
    }
  } catch (err) {
    if (modal) modal.error = cleanError(err);
  } finally {
    if (modal) modal.pickerBusy = false;
    render();
  }
}

function browseRemoteDirectory(): void {
  if (!modal || modal.kind !== 'create' || !modal.remoteDaemonId) return;
  const current = modal;
  openDirPicker({
    language: lang,
    title: tr('projects.remotePath'),
    daemonId: current.remoteDaemonId,
    startPath: current.remotePathInput || current.remotePath || null,
    onSelect: (path) => {
      if (modal !== current) return;
      current.remotePath = path;
      current.remotePathInput = path;
      render();
    },
  });
}

function openProject(projectId: string): void {
  const project = boot?.projects.find((item) => item.id === projectId);
  if (!project?.available || openingProjectId) return;
  openingProjectId = projectId;
  render();
  void (async () => {
    try {
      await bridge.call('projects.open', { projectId });
      window.location.href = `/console/projects/${encodeURIComponent(projectId)}/chat`;
    } catch (err) {
      openingProjectId = null;
      window.alert(cleanError(err));
      render();
    }
  })();
}

async function createProject(): Promise<void> {
  if (!modal || modal.kind !== 'create' || modal.busy) return;
  const current = modal;
  const targetPath = (current.pathInput || current.path).trim();
  const remotePath = (current.remotePathInput || current.remotePath).trim();
  const remoteDaemonId = current.remoteDaemonId;
  if (remoteDaemonId && !remotePath) return;
  current.busy = true;
  current.error = null;
  render();
  try {
    const result = await bridge.call<{ projects: ConsoleProjectInfo[] }>(
      'projects.create',
      {
        path: targetPath,
        name: current.name,
        remoteDaemonId: remoteDaemonId || null,
        remotePath: remoteDaemonId ? remotePath : null,
      },
    );
    boot = { ...boot!, projects: result.projects };
    modal = null;
  } catch (err) {
    current.error = cleanError(err);
  } finally {
    current.busy = false;
    render();
  }
}

async function renameProject(): Promise<void> {
  if (!modal || modal.kind !== 'rename' || modal.busy || !modal.projectId) return;
  const current = modal;
  current.busy = true;
  current.error = null;
  render();
  try {
    const result = await bridge.call<{ projects: ConsoleProjectInfo[] }>(
      'projects.rename',
      { projectId: current.projectId, name: current.name },
    );
    boot = { ...boot!, projects: result.projects };
    modal = null;
  } catch (err) {
    current.error = cleanError(err);
  } finally {
    current.busy = false;
    render();
  }
}

async function deleteProject(): Promise<void> {
  if (!modal || modal.kind !== 'delete' || modal.busy || !modal.projectId) return;
  const current = modal;
  current.busy = true;
  current.error = null;
  render();
  try {
    const result = await bridge.call<{ projects: ConsoleProjectInfo[] }>(
      'projects.delete',
      { projectId: current.projectId },
    );
    boot = { ...boot!, projects: result.projects };
    modal = null;
  } catch (err) {
    current.error = cleanError(err);
  } finally {
    current.busy = false;
    render();
  }
}

function wireEvents(): void {
  document.getElementById('agd-project-add')?.addEventListener('click', openCreate);
  document.getElementById('agd-project-add-empty')?.addEventListener('click', openCreate);
  document.getElementById('agd-projects-settings')?.addEventListener('click', () => void bridge.call('openSettings'));
  document.getElementById('agd-projects-lang')?.addEventListener('click', async () => {
    lang = lang === 'zh' ? 'en' : 'zh';
    render();
    await bridge.call('setLanguage', { language: lang });
  });
  document.querySelectorAll<HTMLElement>('[data-project-open]').forEach((card) => {
    card.addEventListener('click', (event) => {
      if ((event.target as Element).closest('button')) return;
      openProject(card.dataset.projectOpen ?? '');
    });
  });
  document.querySelectorAll<HTMLElement>('[data-project-open-button]').forEach((button) => {
    button.addEventListener('click', () => openProject(button.dataset.projectOpenButton ?? ''));
  });
  document.querySelectorAll<HTMLElement>('[data-project-rename]').forEach((button) => {
    button.addEventListener('click', () => {
      const project = boot?.projects.find((item) => item.id === button.dataset.projectRename);
      if (!project) return;
      modal = { kind: 'rename', busy: false, error: null, projectId: project.id, name: project.name, path: project.path, pathInput: '', pickerBusy: false, remoteDaemonId: '', remotePath: '', remotePathInput: '' };
      render();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-project-delete]').forEach((button) => {
    button.addEventListener('click', () => {
      const project = boot?.projects.find((item) => item.id === button.dataset.projectDelete);
      if (!project) return;
      modal = { kind: 'delete', busy: false, error: null, projectId: project.id, name: project.name, path: project.path, pathInput: '', pickerBusy: false, remoteDaemonId: '', remotePath: '', remotePathInput: '' };
      render();
    });
  });
  document.getElementById('agd-project-modal-close')?.addEventListener('click', () => { if (!modal?.busy) { modal = null; render(); } });
  document.getElementById('agd-project-cancel')?.addEventListener('click', () => { if (!modal?.busy) { modal = null; render(); } });
  document.getElementById('agd-project-modal-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !modal?.busy) { modal = null; render(); }
  });
  document.getElementById('agd-project-name')?.addEventListener('input', (event) => { if (modal) modal.name = (event.target as HTMLInputElement).value; });
  document.getElementById('agd-project-path')?.addEventListener('input', (event) => {
    if (modal) modal.pathInput = (event.target as HTMLInputElement).value;
    const button = document.getElementById('agd-project-create') as HTMLButtonElement | null;
    if (button) {
      button.disabled = !(modal?.pathInput || modal?.path)
        || Boolean(modal?.remoteDaemonId && !(modal!.remotePathInput || modal!.remotePath));
    }
  });
  document.getElementById('agd-project-browse-path')?.addEventListener('click', () => void pickDirectory());
  document.getElementById('agd-project-remote-daemon')?.addEventListener('change', (event) => {
    if (!modal) return;
    modal.remoteDaemonId = (event.target as HTMLSelectElement).value;
    if (!modal.remoteDaemonId) {
      modal.remotePath = '';
      modal.remotePathInput = '';
    }
    render();
  });
  document.getElementById('agd-project-remote-path')?.addEventListener('input', (event) => {
    if (modal) modal.remotePathInput = (event.target as HTMLInputElement).value;
    // The create button's disabled state depends on this field; only the
    // button needs refreshing, and re-rendering would drop focus.
    const button = document.getElementById('agd-project-create') as HTMLButtonElement | null;
    if (button) {
      button.disabled = !(modal?.pathInput || modal?.path)
        || Boolean(modal?.remoteDaemonId && !(modal.remotePathInput || modal.remotePath));
    }
  });
  document.getElementById('agd-project-browse-remote')?.addEventListener('click', browseRemoteDirectory);
  document.getElementById('agd-project-create')?.addEventListener('click', () => void createProject());
  document.getElementById('agd-project-rename-name')?.addEventListener('input', (event) => { if (modal) modal.name = (event.target as HTMLInputElement).value; });
  document.getElementById('agd-project-rename-save')?.addEventListener('click', () => void renameProject());
  document.getElementById('agd-project-delete-confirm')?.addEventListener('click', () => void deleteProject());
  document.getElementById('agd-usage-agent')?.addEventListener('change', () => {
    dashboard.agent = (document.getElementById('agd-usage-agent') as HTMLSelectElement).value;
    void loadUsage();
  });
  document.getElementById('agd-usage-range')?.addEventListener('change', () => {
    dashboard.range = (document.getElementById('agd-usage-range') as HTMLSelectElement).value as UsageRange;
    void loadUsage();
  });
}

async function main(): Promise<void> {
  await refresh();
  void loadUsage();
}

void main();
