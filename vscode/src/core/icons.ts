import claudeSvg from '@lobehub/icons-static-svg/icons/claudecode.svg';
import codexSvg from '@lobehub/icons-static-svg/icons/codex.svg';
import cursorSvg from '@lobehub/icons-static-svg/icons/cursor.svg';
import githubCopilotSvg from '@lobehub/icons-static-svg/icons/githubcopilot.svg';
import kimiSvg from '@lobehub/icons-static-svg/icons/kimi.svg';
import opencodeSvg from '@lobehub/icons-static-svg/icons/opencode.svg';
import openclawSvg from '@lobehub/icons-static-svg/icons/openclaw.svg';
import qwenSvg from '@lobehub/icons-static-svg/icons/qwen.svg';

const ICONS: Record<string, string> = {
  claude: claudeSvg,
  codex: codexSvg,
  cursor: cursorSvg,
  copilot: githubCopilotSvg,
  kimi: kimiSvg,
  opencode: opencodeSvg,
  openclaw: openclawSvg,
  qwen: qwenSvg,
};

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

export function providerColor(provider: string): string {
  return PROVIDER_COLORS[provider.toLowerCase()] ?? '#6E7681';
}

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;');
}

function hasIcon(provider: string): boolean {
  return provider.toLowerCase() in ICONS;
}

/**
 * Render a provider logo as inline SVG for the webviews.
 * Known providers use the LobeHub icon; unknown providers fall back to a
 * colored initial-letter circle to keep the UI consistent.
 */
export function providerLogo(provider: string, size: 'sm' | 'md' = 'sm'): string {
  const key = provider.toLowerCase();
  const svg = ICONS[key];
  const color = providerColor(provider);
  if (svg) {
    const sizeClass = size === 'md' ? 'is-md' : 'is-sm';
    return `<span class="agd-provider-logo ${sizeClass}" style="--c:${color}" aria-label="${escapeHtml(provider)}">${svg}</span>`;
  }
  return `<span class="agd-logo is-${size}" style="--c:${color}">${escapeHtml(provider.charAt(0).toUpperCase())}</span>`;
}

/**
 * Build a data URI for the provider icon so platform code (e.g. the VS Code
 * tree view) can use it as a TreeItem iconPath. The SVG is recolored with the
 * brand color because `currentColor` has no meaning when rendered as a
 * standalone image.
 */
export function providerIconDataUrl(provider: string): string | null {
  const key = provider.toLowerCase();
  const svg = ICONS[key];
  if (!svg) return null;
  const color = providerColor(provider);
  const colored = svg.replaceAll('currentColor', color);
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(colored)}`;
}

export { hasIcon };
