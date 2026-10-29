import './dir-picker.css';
import { t, type MessageKey } from '../core/i18n';
import type { ConsoleDirectoryListing, UiLanguage } from '../core/types';
import { bridge } from './bridge';

/**
 * In-page directory browser shared by the project modal and the three run
 * modes. It lists directories through ``projects.directories``, which the
 * browser host routes either to the Server machine or — when ``daemonId``
 * is set — to that daemon's ``fs.list`` RPC. Roots are the allowlist on the
 * owning machine, so anything selectable is also runnable there.
 */
export interface DirPickerOptions {
  language: UiLanguage;
  title: string;
  /** Remote daemon whose filesystem to browse; omit for the Server machine. */
  daemonId?: string | null;
  /** Directory to open first; omitted → the first allowed root. */
  startPath?: string | null;
  onSelect: (path: string) => void;
}

interface PickerState extends DirPickerOptions {
  daemonId: string | null;
  listing: ConsoleDirectoryListing | null;
  current: string | null;
  input: string;
  loading: boolean;
  error: string | null;
}

const HOST_ID = 'agd-dir-picker-root';

let picker: PickerState | null = null;

function tr(key: MessageKey, vars?: Record<string, string | number>): string {
  return t(picker?.language ?? 'zh', key, vars);
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

function host(): HTMLElement {
  let element = document.getElementById(HOST_ID);
  if (!element) {
    element = document.createElement('div');
    element.id = HOST_ID;
    document.body.append(element);
  }
  return element;
}

export function openDirPicker(options: DirPickerOptions): void {
  picker = {
    ...options,
    daemonId: options.daemonId || null,
    listing: null,
    current: null,
    input: options.startPath || '',
    loading: true,
    error: null,
  };
  render();
  void navigate(options.startPath || null);
}

export function closeDirPicker(): void {
  picker = null;
  host().innerHTML = '';
}

async function navigate(path: string | null): Promise<void> {
  if (!picker) return;
  const target = path ? path.trim() : null;
  if (target) picker.input = target;
  picker.loading = true;
  picker.error = null;
  render();
  try {
    const listing = await bridge.call<ConsoleDirectoryListing>(
      'projects.directories',
      { path: target, daemonId: picker.daemonId },
    );
    if (!picker) return;
    picker.listing = listing;
    picker.current = listing.current;
    picker.input = listing.current;
  } catch (err) {
    if (picker) picker.error = cleanError(err);
  } finally {
    if (picker) {
      picker.loading = false;
      render();
    }
  }
}

function render(): void {
  if (!picker) return;
  const listing = picker.listing;
  const rows = listing?.directories.length
    ? listing.directories.map((item) => `
        <button type="button" class="agd-dir-row" data-dir-path="${esc(item.path)}">
          <span class="agd-dir-icon">▸</span>
          <span class="agd-mono">${esc(item.name)}</span>
        </button>`).join('')
    : '';
  const body = picker.loading
    ? '<div class="agd-dir-empty">…</div>'
    : picker.error
      ? `<div class="agd-dir-empty agd-dir-error">${esc(picker.error)}</div>`
      : rows || `<div class="agd-dir-empty">${esc(tr('dirpicker.empty'))}</div>`;

  host().innerHTML = `
    <div class="agd-modal-backdrop agd-dir-backdrop" id="agd-dir-backdrop">
      <section class="agd-modal agd-dir-picker" role="dialog" aria-modal="true">
        <header class="agd-modal-head">
          <div><h3>${esc(picker.title)}</h3><p>${esc(tr(picker.daemonId ? 'dirpicker.remoteHint' : 'dirpicker.localHint'))}</p></div>
          <button type="button" class="agd-btn" id="agd-dir-close">×</button>
        </header>
        <div class="agd-dir-bar">
          <button type="button" class="agd-btn" id="agd-dir-up" ${listing?.parent ? '' : 'disabled'} title="${esc(tr('dirpicker.up'))}">↑</button>
          <input id="agd-dir-input" class="agd-input agd-mono" value="${esc(picker.input)}" placeholder="/absolute/path">
          <button type="button" class="agd-btn" id="agd-dir-go">${esc(tr('dirpicker.go'))}</button>
        </div>
        <div class="agd-dir-list">${body}</div>
        <div class="agd-dir-current agd-mono" title="${esc(picker.current ?? '')}">${esc(picker.current ?? '')}</div>
        <div class="agd-actions agd-modal-actions">
          <button type="button" class="agd-btn" id="agd-dir-cancel">${esc(tr('settings.cancel'))}</button>
          <span class="agd-flex"></span>
          <button type="button" class="agd-btn agd-btn-primary" id="agd-dir-select" ${picker.current ? '' : 'disabled'}>${esc(tr('dirpicker.select'))}</button>
        </div>
      </section>
    </div>`;
  wire();
}

function wire(): void {
  const root = host();
  root.querySelectorAll<HTMLElement>('[data-dir-path]').forEach((row) => {
    row.addEventListener('click', () => void navigate(row.dataset.dirPath ?? ''));
  });
  document.getElementById('agd-dir-up')?.addEventListener('click', () => {
    if (picker?.listing?.parent) void navigate(picker.listing.parent);
  });
  document.getElementById('agd-dir-go')?.addEventListener('click', () => {
    const value = (document.getElementById('agd-dir-input') as HTMLInputElement | null)?.value ?? '';
    void navigate(value.trim());
  });
  document.getElementById('agd-dir-input')?.addEventListener('keydown', (event) => {
    const key = (event as KeyboardEvent).key;
    if (key !== 'Enter') return;
    event.preventDefault();
    const value = (event.target as HTMLInputElement).value;
    void navigate(value.trim());
  });
  document.getElementById('agd-dir-select')?.addEventListener('click', () => {
    if (!picker?.current) return;
    const chosen = picker.current;
    const onSelect = picker.onSelect;
    closeDirPicker();
    onSelect(chosen);
  });
  document.getElementById('agd-dir-close')?.addEventListener('click', closeDirPicker);
  document.getElementById('agd-dir-cancel')?.addEventListener('click', closeDirPicker);
  document.getElementById('agd-dir-backdrop')?.addEventListener('mousedown', (event) => {
    if (event.target === event.currentTarget) closeDirPicker();
  });
}
