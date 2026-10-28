import './mode-switcher.css';
import './styles.css';
import './projects.css';
import { t, type MessageKey } from '../core/i18n';
import type {
  ConsoleProjectInfo,
  ServerState,
  UiLanguage,
} from '../core/types';
import { bridge } from './bridge';

interface ProjectsBoot {
  language: UiLanguage;
  state: ServerState;
  projects: ConsoleProjectInfo[];
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
}

let boot: ProjectsBoot | null = null;
let lang: UiLanguage = 'zh';
let modal: ProjectModal | null = null;
let openingProjectId: string | null = null;

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

function renderProject(project: ConsoleProjectInfo): string {
  const busy = openingProjectId === project.id;
  return `
    <article class="agd-project-card ${project.available ? '' : 'is-unavailable'}" data-project-open="${esc(project.id)}">
      <div class="agd-project-icon">⌂</div>
      <div class="agd-project-main">
        <div class="agd-project-title-line">
          <strong>${esc(project.name)}</strong>
          <span class="agd-pill ${project.available ? 'agd-pill-ok' : 'agd-pill-err'}">${project.available ? tr('projects.available') : tr('projects.unavailable')}</span>
        </div>
        <div class="agd-project-path agd-mono" title="${esc(project.path)}">${esc(project.path)}</div>
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
        ${modal.error ? `<p class="agd-form-err">${esc(modal.error)}</p>` : ''}
        <div class="agd-actions agd-modal-actions">
          <button type="button" class="agd-btn" id="agd-project-cancel" ${modal.busy ? 'disabled' : ''}>${tr('settings.cancel')}</button>
          <span class="agd-flex"></span>
          <button type="button" class="agd-btn agd-btn-primary" id="agd-project-create" ${modal.busy || !(modal.pathInput || modal.path) ? 'disabled' : ''}>${modal.busy ? tr('projects.creating') : tr('projects.create')}</button>
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
  current.busy = true;
  current.error = null;
  render();
  try {
    const result = await bridge.call<{ projects: ConsoleProjectInfo[] }>(
      'projects.create',
      { path: targetPath, name: current.name },
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
      modal = { kind: 'rename', busy: false, error: null, projectId: project.id, name: project.name, path: project.path, pathInput: '', pickerBusy: false };
      render();
    });
  });
  document.querySelectorAll<HTMLElement>('[data-project-delete]').forEach((button) => {
    button.addEventListener('click', () => {
      const project = boot?.projects.find((item) => item.id === button.dataset.projectDelete);
      if (!project) return;
      modal = { kind: 'delete', busy: false, error: null, projectId: project.id, name: project.name, path: project.path, pathInput: '', pickerBusy: false };
      render();
    });
  });
  document.getElementById('agd-project-modal-close')?.addEventListener('click', () => { if (!modal?.busy) { modal = null; render(); } });
  document.getElementById('agd-project-cancel')?.addEventListener('click', () => { if (!modal?.busy) { modal = null; render(); } });
  document.getElementById('agd-project-modal-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget && !modal?.busy) { modal = null; render(); }
  });
  document.getElementById('agd-project-name')?.addEventListener('input', (event) => { if (modal) modal.name = (event.target as HTMLInputElement).value; });
  document.getElementById('agd-project-path')?.addEventListener('input', (event) => { if (modal) modal.pathInput = (event.target as HTMLInputElement).value; });
  document.getElementById('agd-project-browse-path')?.addEventListener('click', () => void pickDirectory());
  document.getElementById('agd-project-create')?.addEventListener('click', () => void createProject());
  document.getElementById('agd-project-rename-name')?.addEventListener('input', (event) => { if (modal) modal.name = (event.target as HTMLInputElement).value; });
  document.getElementById('agd-project-rename-save')?.addEventListener('click', () => void renameProject());
  document.getElementById('agd-project-delete-confirm')?.addEventListener('click', () => void deleteProject());
}

async function main(): Promise<void> {
  await refresh();
}

void main();
