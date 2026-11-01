import { isLocalUrl } from './sop/run';
import type { DaemonInfo } from './types';

/** One agent provider selectable in console filters. */
export interface AgentOption {
  provider: string;
  label: string;
}

/**
 * Union of the runtime providers probed on every known daemon.
 *
 * Daemons report their last-known providers even while offline, so this
 * stays stable across a daemon restart. Rows are deduped by provider key;
 * the first non-empty display name wins.
 */
export function agentOptionsFromDaemons(daemons: DaemonInfo[]): AgentOption[] {
  const byProvider = new Map<string, AgentOption>();
  for (const daemon of daemons) {
    for (const runtime of daemon.providers ?? []) {
      if (!runtime.provider || byProvider.has(runtime.provider)) continue;
      byProvider.set(runtime.provider, {
        provider: runtime.provider,
        label: runtime.name?.trim() || runtime.provider,
      });
    }
  }
  return [...byProvider.values()].sort((a, b) => a.label.localeCompare(b.label));
}

/**
 * A daemon on this machine shares the Server's filesystem.
 *
 * A daemon with no reverse-dial URL claims over HTTP long-poll — only local
 * daemons do — so it is local by definition. A configured URL is local when
 * its host is loopback, the wildcard ``0.0.0.0`` / ``::`` bind (how
 * ``~/.agt/daemons`` registers "this machine"), or this machine's hostname.
 * The same rule as ``isLocalUrl`` in ``core/sop/run.ts`` keeps the two
 * classifications from drifting apart.
 */
export function isLocalDaemon(daemon: DaemonInfo): boolean {
  if (!daemon.url) return true;
  const hostname = typeof window === 'undefined' ? '' : window.location.hostname;
  return isLocalUrl(daemon.url, hostname);
}
