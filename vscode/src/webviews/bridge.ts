import { mockCall } from './mock';
import { browserCall } from './browser-host';
import type {
  DaemonInfo,
  IdentityWithBindings,
  ServerState,
  UiLanguage,
} from '../core/types';

export interface BootInfo {
  serverUrl: string;
  autoStart: boolean;
  startCommand: string;
  stopCommand: string;
  autoStartDaemon: boolean;
  daemonStartCommand: string;
  daemonStopCommand: string;
  daemonRunning: boolean;
  language: UiLanguage;
  state: ServerState;
  latencyMs: number | null;
  daemons: DaemonInfo[];
  identities: IdentityWithBindings[];
  localDaemonId: string | null;
  workspaceRoot?: string;
  capabilities?: {
    lifecycleControl?: boolean;
    daemonLifecycle?: boolean;
    addDaemon?: boolean;
    pairing?: boolean;
    console?: boolean;
  };
}

declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
  getState<T = unknown>(): T | undefined;
  setState<T>(state: T): void;
};

interface VsCodeStub {
  postMessage(message: unknown): void;
  getState<T = unknown>(): T | undefined;
  setState<T>(state: T): void;
}

type EventListener = (event: string, payload: unknown) => void;

const vscodeApi: VsCodeStub | undefined =
  typeof acquireVsCodeApi === 'function' ? acquireVsCodeApi() : undefined;
const browserParams = new URLSearchParams(window.location.search);
const browserConsole = vscodeApi === undefined
  && window.location.pathname.startsWith('/console');
const browserMock = vscodeApi === undefined
  && (browserParams.get('mock') === '1' || window.location.port === '5175')
  && !browserConsole;

let requestSeq = 0;
const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: unknown) => void }>();
const eventListeners = new Set<EventListener>();
let mockState: unknown = undefined;

if (vscodeApi) {
  window.addEventListener('message', (e: MessageEvent) => {
    const msg = e.data as
      | { type: 'response'; id: number; result?: unknown; error?: string }
      | { type: 'event'; event: string; payload?: unknown }
      | undefined;
    if (!msg || typeof msg !== 'object') {
      return;
    }
    if (msg.type === 'response') {
      const entry = pending.get(msg.id);
      if (!entry) {
        return;
      }
      pending.delete(msg.id);
      if (msg.error !== undefined) {
        entry.reject(new Error(msg.error));
      } else {
        entry.resolve(msg.result);
      }
      return;
    }
    if (msg.type === 'event') {
      for (const listener of eventListeners) {
        listener(msg.event, msg.payload);
      }
    }
  });
}

export const bridge = {
  /** True when running in a plain browser (npm run dev:web). */
  isMock: browserMock,

  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
    if (browserConsole) {
      return browserCall<T>(method, params, (event, payload) => {
        for (const listener of eventListeners) listener(event, payload);
      });
    }
    if (!vscodeApi) {
      return mockCall<T>(method, params);
    }
    const id = ++requestSeq;
    return new Promise<T>((resolve, reject) => {
      pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      vscodeApi.postMessage({ type: 'request', id, method, params });
    });
  },

  onEvent(listener: EventListener): () => void {
    eventListeners.add(listener);
    return () => eventListeners.delete(listener);
  },

  getState<T = unknown>(): T | undefined {
    if (!vscodeApi) {
      return mockState as T | undefined;
    }
    return vscodeApi.getState<T>();
  },

  setState<T>(state: T): void {
    if (!vscodeApi) {
      mockState = state;
      return;
    }
    vscodeApi.setState(state);
  },
};
