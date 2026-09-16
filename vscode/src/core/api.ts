import type {
  ChatSessionInfo,
  DaemonInfo,
  IdentityBindingInfo,
  IdentityInfo,
  IdentityWithBindings,
  JobInfo,
  JobStatusInfo,
  RuntimeInfo,
  SessionHistoryInfo,
  SessionResolveInfo,
  SopValidation,
  TaskStatusInfo,
  TaskStreamEvent,
} from './types';

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

  private async request<T>(
    path: string,
    init: RequestInit = {},
    timeoutMs = DEFAULT_TIMEOUT_MS,
  ): Promise<T> {
    const controller = new AbortController();
    let onAbort: (() => void) | undefined;
    if (init.signal) {
      const outer = init.signal;
      if (outer.aborted) controller.abort(outer.reason);
      else {
        onAbort = () => controller.abort(outer.reason);
        outer.addEventListener('abort', onAbort, { once: true });
      }
    }
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        ...init,
        signal: controller.signal,
        headers: { accept: 'application/json', ...(init.headers ?? {}) },
      });
      if (!res.ok) {
        throw new Error(await errorMessage(res));
      }
      return (await res.json()) as T;
    } finally {
      if (init.signal && onAbort) init.signal.removeEventListener('abort', onAbort);
      clearTimeout(timer);
    }
  }

  private post<T>(path: string, body: unknown, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
    return this.request<T>(
      path,
      {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      },
      timeoutMs,
    );
  }

  async health(): Promise<{ status: string }> {
    return this.request<{ status: string }>('/api/health', {}, 2500);
  }

  async daemons(): Promise<DaemonInfo[]> {
    const res = await this.request<{ daemons: DaemonInfo[] }>('/api/daemons');
    return res.daemons;
  }

  async runtimes(): Promise<RuntimeInfo[]> {
    const res = await this.request<{ runtimes: RuntimeInfo[] }>('/api/runtimes');
    return res.runtimes;
  }

  async identities(): Promise<IdentityInfo[]> {
    const res = await this.request<{ identities: IdentityInfo[] }>('/api/identities');
    return res.identities;
  }

  async identityBindings(did: string): Promise<IdentityBindingInfo[]> {
    const didPath = encodeURIComponent(did);
    const res = await this.request<{ bindings: IdentityBindingInfo[] }>(
      `/api/identities/${didPath}/bindings`,
    );
    return res.bindings;
  }

  async identitiesWithBindings(): Promise<IdentityWithBindings[]> {
    const identities = await this.identities();
    return Promise.all(
      identities.map(async (identity) => ({
        ...identity,
        bindings: await this.identityBindings(identity.did),
      })),
    );
  }

  /**
   * Server-side SOP validation (authority for the DSL rules).
   *
   * Takes the document **as authored** — the editor holds the YAML text,
   * and parsing stays a single Python implementation — and always answers
   * 200: a failed validation arrives as ``ok: false`` + ``error``.
   */
  async validateSop(raw: string): Promise<SopValidation> {
    return this.post<SopValidation>('/api/sop/validate', { raw });
  }

  async daemonSessions(daemonId: string): Promise<ChatSessionInfo[]> {
    const id = encodeURIComponent(daemonId);
    const res = await this.request<{ sessions: ChatSessionInfo[] }>(
      `/api/daemons/${id}/sessions`,
    );
    return res.sessions;
  }

  async resolveSession(sessionId: string): Promise<SessionResolveInfo> {
    return this.request<SessionResolveInfo>(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
    );
  }

  async sessionHistory(sessionId: string): Promise<SessionHistoryInfo> {
    return this.request<SessionHistoryInfo>(
      `/api/sessions/${encodeURIComponent(sessionId)}/history`,
    );
  }

  async createChatJob(input: {
    prompt: string;
    provider: string;
    targetDaemonId: string;
    identityDid?: string | null;
    agentCwd?: string | null;
  }): Promise<JobInfo> {
    return this.post<JobInfo>('/api/jobs', {
      prompt: input.prompt,
      provider: input.provider,
      target_daemon_id: input.targetDaemonId,
      identity_did: input.identityDid ?? null,
      agent_cwd: input.agentCwd ?? null,
    });
  }

  async appendChatInput(jobId: string, prompt: string): Promise<JobInfo> {
    return this.post<JobInfo>(`/api/jobs/${encodeURIComponent(jobId)}/input`, { prompt });
  }

  async jobStatus(jobId: string): Promise<JobStatusInfo> {
    return this.request<JobStatusInfo>(`/api/jobs/${encodeURIComponent(jobId)}/status`);
  }

  async cancelJob(jobId: string): Promise<{ cancelled: boolean }> {
    return this.post<{ cancelled: boolean }>(
      `/api/jobs/${encodeURIComponent(jobId)}/cancel`,
      {},
    );
  }

  async taskStatus(taskId: string): Promise<TaskStatusInfo> {
    return this.request<TaskStatusInfo>(`/api/tasks/${encodeURIComponent(taskId)}/status`);
  }

  async *streamTask(taskId: string, signal?: AbortSignal): AsyncGenerator<TaskStreamEvent> {
    const res = await fetch(
      `${this.baseUrl}/api/tasks/${encodeURIComponent(taskId)}/stream`,
      { signal, headers: { accept: 'text/event-stream' } },
    );
    if (!res.ok) {
      throw new Error(await errorMessage(res));
    }
    if (!res.body) {
      throw new Error('server returned an empty event stream');
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf('\n\n');
      while (boundary >= 0) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const event = parseSseFrame(frame);
        if (event) yield event;
        boundary = buffer.indexOf('\n\n');
      }
    }
    buffer += decoder.decode();
    const event = parseSseFrame(buffer);
    if (event) yield event;
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

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    const detail = body.detail ?? body.message;
    if (typeof detail === 'string' && detail.trim()) {
      return `HTTP ${res.status}: ${detail}`;
    }
    if (detail) {
      return `HTTP ${res.status}: ${JSON.stringify(detail)}`;
    }
  } catch {
    // Non-JSON error responses keep the status-line fallback below.
  }
  return `HTTP ${res.status} ${res.statusText}`;
}

/**
 * True when a request failed because the server has no such route.
 *
 * The usual cause during development is a server process that predates the
 * endpoint (started from an older install/build); callers turn this into an
 * actionable "restart the server" message instead of a bare 404.
 */
export function isMissingEndpoint(err: unknown): boolean {
  return /\bHTTP 404\b/.test(String(err));
}

function parseSseFrame(frame: string): TaskStreamEvent | undefined {
  let type = 'message';
  let data = '';
  for (const rawLine of frame.split(/\r?\n/)) {
    if (rawLine.startsWith(':')) continue;
    if (rawLine.startsWith('event:')) {
      type = rawLine.slice(6).trim();
    } else if (rawLine.startsWith('data:')) {
      data += rawLine.slice(5).trim();
    }
  }
  if (!data || (type !== 'snapshot' && type !== 'delta' && type !== 'done')) {
    return undefined;
  }
  const payload = JSON.parse(data) as Record<string, unknown>;
  return { type, ...(payload as object) } as TaskStreamEvent;
}
