import { isLocalUrl } from './sop/run';
import type { DaemonInfo } from './types';

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
