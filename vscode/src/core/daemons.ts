import type { DaemonInfo } from './types';

/** A daemon on this machine shares the Server's filesystem. */
export function isLocalDaemon(daemon: DaemonInfo): boolean {
  const host = new URL(daemon.url ?? window.location.origin).hostname;
  return ['127.0.0.1', 'localhost', '::1', window.location.hostname].includes(host);
}
