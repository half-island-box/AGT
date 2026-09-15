import { mockCall } from './mock';
import type { DaemonInfo, ServerState, UiLanguage } from '../core/types';

export interface BootInfo {
  serverUrl: string;
  autoStart: boolean;
  startCommand: string;
  stopCommand: string;
  language: UiLanguage;
  state: ServerState;
  latencyMs: number | null;
  daemons: DaemonInfo[];
}

declare function acquireVsCodeApi(): {
  postMessage(message: unknown): void;
};

interface VsCodeStub {
  postMessage(message: unknown): void;
}

type EventListener = (event: string, payload: unknown) => void;

const vscodeApi: VsCodeStub | undefined =
  typeof acquireVsCodeApi === 'function' ? acquireVsCodeApi() : undefined;

let requestSeq = 0;
const pending = new Map<number, { resolve: (v: unknown) => void; reject: (e: unknown) => void }>();
const eventListeners = new Set<EventListener>();

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
  isMock: vscodeApi === undefined,

  call<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T> {
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
};
