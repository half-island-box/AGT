const PYPI_URL = 'https://pypi.org/pypi/agt-team/json';
const CACHE_TTL_MS = 60 * 60 * 1000;

let cached: { version: string; at: number } | null = null;

/** Extract ``info.version`` from a PyPI JSON response. Malformed → ``null``. */
export function parseLatestVersion(payload: unknown): string | null {
  if (!payload || typeof payload !== 'object') return null;
  const info = (payload as { info?: unknown }).info;
  if (!info || typeof info !== 'object') return null;
  const version = (info as { version?: unknown }).version;
  return typeof version === 'string' && version.trim() ? version.trim() : null;
}

/**
 * Latest published ``agt-team`` version, or ``null`` when PyPI is unreachable.
 * Cached for an hour so repeated activations do not hammer the index.
 */
export async function fetchLatestVersion(timeoutMs = 5000): Promise<string | null> {
  if (cached && Date.now() - cached.at < CACHE_TTL_MS) {
    return cached.version;
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(PYPI_URL, {
      signal: controller.signal,
      headers: { accept: 'application/json' },
    });
    if (!response.ok) return null;
    const version = parseLatestVersion(await response.json());
    if (version) cached = { version, at: Date.now() };
    return version;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
