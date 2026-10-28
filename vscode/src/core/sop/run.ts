/**
 * Run-tracking helpers: which daemon is "this machine" for `local_daemon_id`,
 * and a cheap document fingerprint so a running SOP can be compared against
 * the template it was submitted from.
 *
 * Pure functions (no `vscode`, no IO) so they are unit-testable.
 */

const LOCAL_HOSTS = new Set(['0.0.0.0', '127.0.0.1', '::', '::1', 'localhost']);

/** Host part of a `ws://host:port` / `http://host:port` URL (``''`` if unparsable). */
export function hostOfUrl(url: string | null | undefined): string {
  if (!url) {
    return '';
  }
  try {
    return new URL(url).hostname;
  } catch {
    return '';
  }
}

/**
 * ``true`` when the URL points at this machine (loopback, wildcard bind, or
 * the given hostname). Mirrors `agt/net.py::is_local_host` closely enough for
 * choosing which daemon id to send as `local_daemon_id`.
 */
export function isLocalUrl(
  url: string | null | undefined,
  hostname = '',
): boolean {
  const host = hostOfUrl(url).replace(/^\[|\]$/g, '');
  if (host === '') {
    return false;
  }
  return LOCAL_HOSTS.has(host) || (hostname !== '' && host === hostname);
}

/**
 * The daemon id this machine registers under, picked from the server's
 * daemon list — the extension must not re-derive `uuid5(namespace, url)`
 * in TypeScript (that identity derivation lives in Python).
 */
export function localDaemonId(
  daemons: { daemon_id: string; url: string | null }[],
  hostname = '',
): string | null {
  const hit = daemons.find((daemon) => isLocalUrl(daemon.url, hostname));
  return hit ? hit.daemon_id : null;
}

/** FNV-1a — a stable fingerprint for "is this the template we ran?". */
export function fingerprint(text: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, '0');
}

export type SopRunPhase = 'idle' | 'running' | 'terminal';

/** Classify a status snapshot: is anything still moving? */
export function runPhase(allTerminal: boolean, hasRun: boolean): SopRunPhase {
  if (!hasRun) {
    return 'idle';
  }
  return allTerminal ? 'terminal' : 'running';
}
