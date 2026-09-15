/** Mirror of the agt server API shapes used by the extension. */

export interface DaemonProvider {
  provider: string;
  name: string;
  version: string;
  status: string;
  last_seen: string;
}

export interface DaemonInfo {
  daemon_id: string;
  daemon_name: string | null;
  device_name: string;
  providers: DaemonProvider[];
  runtime_count: number;
  last_seen: string;
  online: boolean;
  url: string | null;
  capabilities: string[];
}

export interface RuntimeInfo {
  name: string;
  type: string;
  version: string;
  status: string;
  device_name: string;
  daemon_id: string;
  daemon_name: string | null;
}

export type ServerState = 'unknown' | 'offline' | 'starting' | 'online';

export type UiLanguage = 'zh' | 'en';
