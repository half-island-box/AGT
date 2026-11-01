/**
 * Usage view (activity bar, below the Daemons tree).
 *
 * Mirrors the console dashboard's data — the same filters, totals and
 * daemon × agent × model breakdown — but drops the trend chart and tightens
 * the layout for a narrow sidebar.
 */

import './styles.css';
import './usage.css';
import { t, type MessageKey } from '../core/i18n';
import type { AgentOption } from '../core/daemons';
import { fmtUsageNum, usageWindow, type UsageRange } from '../core/usage';
import type { ServerState, UiLanguage, UsageSummary } from '../core/types';
import { bridge } from './bridge';

interface UsageBoot {
  language: UiLanguage;
  state: ServerState;
  agents: AgentOption[];
  /** ``daemon_id → human-readable name`` for every known daemon. */
  daemonNames: Record<string, string>;
}

interface UsageFilters {
  agent: string; // 'all' or a provider key
  range: UsageRange;
}

let boot: UsageBoot | null = null;
let lang: UiLanguage = 'zh';
let filters: UsageFilters = { agent: 'all', range: '30d' };
let data: UsageSummary | null = null;
let loading = true;
let error: string | null = null;
let requestSeq = 0;

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

/** Name for a daemon in usage rows; historical ids may no longer be known. */
function daemonDisplayName(daemonId: string | null): string {
  if (!daemonId) return tr('projects.usage.unknownDaemon');
  return boot?.daemonNames[daemonId] ?? daemonId;
}

function agentLabel(agent: string): string {
  return boot?.agents.find((option) => option.provider === agent)?.label ?? agent;
}

function renderFilters(): string {
  const agentChoices = [
    { value: 'all', label: tr('projects.usage.agentAll') },
    ...(boot?.agents ?? []).map((option) => ({
      value: option.provider,
      label: option.label,
    })),
  ];
  const agentOptions = agentChoices.map(({ value, label }) => {
    const selected = filters.agent === value ? ' selected' : '';
    return `<option value="${esc(value)}"${selected}>${esc(label)}</option>`;
  }).join('');

  const ranges: Array<{ value: UsageRange; label: string }> = [
    { value: 'today', label: tr('projects.usage.rangeToday') },
    { value: '7d', label: tr('projects.usage.range7d') },
    { value: '30d', label: tr('projects.usage.range30d') },
    { value: '90d', label: tr('projects.usage.range90d') },
    { value: 'all', label: tr('projects.usage.rangeAll') },
  ];
  const rangeOptions = ranges.map((range) => {
    const selected = filters.range === range.value ? ' selected' : '';
    return `<option value="${range.value}"${selected}>${esc(range.label)}</option>`;
  }).join('');

  return `
    <div class="agd-usage-filters">
      <label class="agd-usage-filter">
        <span>${esc(tr('projects.usage.agent'))}</span>
        <select id="agd-usage-agent" class="agd-input">${agentOptions}</select>
      </label>
      <label class="agd-usage-filter">
        <span>${esc(tr('projects.usage.range'))}</span>
        <select id="agd-usage-range" class="agd-input">${rangeOptions}</select>
      </label>
    </div>`;
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

function renderBreakdown(items: UsageSummary['breakdown']): string {
  if (!items.length) return '';
  const rows = items.map((item) => `
    <tr>
      <td>${esc(daemonDisplayName(item.daemon_id))}</td>
      <td>${esc(agentLabel(item.agent))}</td>
      <td class="agd-mono">${esc(item.model ?? '—')}</td>
      <td class="agd-num">${fmtUsageNum(item.input_tokens)}</td>
      <td class="agd-num">${fmtUsageNum(item.output_tokens)}</td>
      <td class="agd-num">${fmtUsageNum(item.cache_read_input_tokens)}</td>
      <td class="agd-num">${fmtUsageNum(item.steps)}</td>
    </tr>`).join('');
  return `
    <div class="agd-usage-breakdown">
      <h4>${esc(tr('projects.usage.breakdown'))}</h4>
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
      </div>
    </div>`;
}

function renderBody(): string {
  if (boot && boot.state !== 'online') {
    return `<div class="agd-usage-empty">${esc(tr('chat.serverOffline'))}</div>`;
  }
  if (loading) {
    return `<div class="agd-usage-empty">${spinner()}${esc(tr('projects.usage.loading'))}</div>`;
  }
  if (error) {
    return `<div class="agd-usage-empty agd-usage-error">${esc(error)}</div>`;
  }
  if (!data || data.totals.steps === 0) {
    return `<div class="agd-usage-empty">${esc(tr('projects.usage.empty'))}</div>`;
  }
  const totals = data.totals;
  const cards = [
    renderStatCard(tr('projects.usage.input'), fmtUsageNum(totals.input_tokens), '↓'),
    renderStatCard(tr('projects.usage.output'), fmtUsageNum(totals.output_tokens), '↑'),
    renderStatCard(tr('projects.usage.cacheRead'), fmtUsageNum(totals.cache_read_input_tokens), '↻'),
    renderStatCard(tr('projects.usage.steps'), fmtUsageNum(totals.steps), '#'),
  ].join('');
  return `
    <div class="agd-usage-cards">${cards}</div>
    ${renderBreakdown(data.breakdown)}`;
}

function wire(): void {
  document.getElementById('agd-usage-agent')?.addEventListener('change', (event) => {
    filters.agent = (event.target as HTMLSelectElement).value;
    bridge.setState(filters);
    void load();
  });
  document.getElementById('agd-usage-range')?.addEventListener('change', (event) => {
    filters.range = (event.target as HTMLSelectElement).value as UsageRange;
    bridge.setState(filters);
    void load();
  });
  document.getElementById('agd-usage-refresh')?.addEventListener('click', () => {
    void bootView();
  });
}

function render(): void {
  const root = document.getElementById('agt-root');
  if (!root) return;
  root.innerHTML = `
    <div class="agd-usage-view">
      <header class="agd-usage-head">
        <h2>${esc(tr('projects.usage.title'))}</h2>
        <button
          type="button"
          id="agd-usage-refresh"
          class="agd-usage-refresh"
          title="${esc(tr('chat.refresh'))}"
          aria-label="${esc(tr('chat.refresh'))}"
          ${loading || boot?.state !== 'online' ? 'disabled' : ''}
        >↻</button>
      </header>
      ${renderFilters()}
      <div class="agd-usage-body">${renderBody()}</div>
    </div>`;
  wire();
}

async function load(): Promise<void> {
  if (bridge.isMock) return;
  const seq = ++requestSeq;
  loading = true;
  error = null;
  render();
  const { from, to, bucket } = usageWindow(filters.range);
  try {
    const summary = await bridge.call<UsageSummary>('usage.summary', {
      agent: filters.agent === 'all' ? null : filters.agent,
      from,
      to,
      bucket,
    });
    if (seq !== requestSeq) return;
    data = summary;
  } catch (err) {
    if (seq !== requestSeq) return;
    error = cleanError(err);
  } finally {
    if (seq === requestSeq) {
      loading = false;
      render();
    }
  }
}

async function bootView(): Promise<void> {
  const saved = bridge.getState<UsageFilters>();
  if (
    saved
    && saved.agent !== undefined
    && saved.range !== undefined
  ) {
    filters = { agent: saved.agent, range: saved.range };
  }
  render();
  try {
    boot = await bridge.call<UsageBoot>('usage.boot');
  } catch (err) {
    loading = false;
    error = cleanError(err);
    render();
    return;
  }
  lang = boot.language;
  if (filters.agent !== 'all' && !boot.agents.some((o) => o.provider === filters.agent)) {
    filters.agent = 'all';
  }
  if (boot.state !== 'online') {
    loading = false;
    data = null;
    error = null;
    render();
    return;
  }
  render();
  await load();
}

bridge.onEvent((event, payload) => {
  if (event === 'refresh') {
    void bootView();
  } else if (event === 'language') {
    lang = payload as UiLanguage;
    render();
  }
});

void bootView();
