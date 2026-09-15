import type { DaemonInfo, RuntimeInfo } from './types';

const DEFAULT_TIMEOUT_MS = 4000;

export function normalizeBaseUrl(url: string): string {
  return url.trim().replace(/\/+$/, '');
}

/** Minimal HTTP client for the agt server. No runtime dependencies. */
export class AgtApi {
  private baseUrl: string;

  constructor(baseUrl: string) {
    this.baseUrl = normalizeBaseUrl(baseUrl);
  }

  setBaseUrl(url: string): void {
    this.baseUrl = normalizeBaseUrl(url);
  }

  private async request<T>(path: string, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        signal: controller.signal,
        headers: { accept: 'application/json' },
      });
      if (!res.ok) {
        throw new Error(`HTTP ${res.status} ${res.statusText}`);
      }
      return (await res.json()) as T;
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<{ status: string }> {
    return this.request<{ status: string }>('/api/health', 2500);
  }

  async daemons(): Promise<DaemonInfo[]> {
    const res = await this.request<{ daemons: DaemonInfo[] }>('/api/daemons');
    return res.daemons;
  }

  async runtimes(): Promise<RuntimeInfo[]> {
    const res = await this.request<{ runtimes: RuntimeInfo[] }>('/api/runtimes');
    return res.runtimes;
  }

  /** Returns latency in ms, or null when the server is unreachable. */
  async ping(): Promise<number | null> {
    const started = Date.now();
    try {
      await this.health();
      return Date.now() - started;
    } catch {
      return null;
    }
  }
}
