/**
 * Usage-dashboard windowing shared by the browser console's project page and
 * the extension's Usage view, so both filters select the same period.
 *
 * Pure functions (no `vscode`, no IO) so they stay unit-testable and keep the
 * two frontends from drifting apart.
 */

export type UsageRange = 'today' | '7d' | '30d' | '90d' | 'all';

export interface UsageWindow {
  from: string;
  to: string;
  bucket: 'hour' | 'day' | 'month';
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** ``YYYY-MM-DD HH:MM:SS`` in UTC — the format `/api/usage/summary` parses. */
export function formatUsageTs(d: Date): string {
  return `${d.getUTCFullYear()}-${pad2(d.getUTCMonth() + 1)}-${pad2(d.getUTCDate())} `
    + `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}:${pad2(d.getUTCSeconds())}`;
}

/**
 * Absolute window for a range filter.
 *
 * ``today`` and ``all`` switch the series bucket too: an hourly series for a
 * single day, a monthly one for the whole history. The totals are unaffected
 * by the bucket, so the plugin view (which has no chart) still uses this.
 */
export function usageWindow(range: UsageRange, now: Date = new Date()): UsageWindow {
  const to = now;
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
  return { from: formatUsageTs(from), to: formatUsageTs(to), bucket };
}

/** Thousands-separated token/step counts for cards and tables. */
export function fmtUsageNum(value: number): string {
  return value.toLocaleString();
}
