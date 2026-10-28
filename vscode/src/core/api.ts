import type {
  ChatInputMessage,
  ChatSessionInfo,
  DaemonConfigInfo,
  ArtifactInfo,
  DaemonInfo,
  ConsoleDirectoryListing,
  ConsoleProjectInfo,
  IdentityBindingInfo,
  IdentityCreateInput,
  IdentityInfo,
  IdentityUpdateInput,
  IdentityWithBindings,
  InputUploadInfo,
  GoalJobTreeNode,
  GoalRunListInfo,
  GoalRunInfo,
  GoalSubmitResult,
  JobLogInfo,
  JobInfo,
  JobStatusInfo,
  RuntimeInfo,
  RunListInfo,
  SessionHistoryInfo,
  SessionResolveInfo,
  SopNodeLog,
  SopStatus,
  SopSubmitResult,
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

  private put<T>(path: string, body: unknown, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
    return this.request<T>(
      path,
      {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      },
      timeoutMs,
    );
  }

  private patch<T>(path: string, body: unknown, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<T> {
    return this.request<T>(
      path,
      {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(body),
      },
      timeoutMs,
    );
  }

  private async delete(path: string, timeoutMs = DEFAULT_TIMEOUT_MS): Promise<void> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const res = await fetch(`${this.baseUrl}${path}`, {
        method: 'DELETE',
        signal: controller.signal,
        headers: { accept: 'application/json' },
      });
      if (!res.ok) {
        throw new Error(await errorMessage(res));
      }
    } finally {
      clearTimeout(timer);
    }
  }

  async health(): Promise<{ status: string }> {
    return this.request<{ status: string }>('/api/health', {}, 2500);
  }

  async daemons(): Promise<DaemonInfo[]> {
    const res = await this.request<{ daemons: DaemonInfo[] }>('/api/daemons');
    return res.daemons;
  }

  async daemonConfig(): Promise<DaemonConfigInfo> {
    return this.request<DaemonConfigInfo>('/api/daemon/config', {}, 10_000);
  }

  async setDaemonConfig(input: {
    maxConcurrent?: number;
    agentTimeout?: number;
    workDir?: string;
    agentCwdAllowlist?: string[];
  }): Promise<DaemonConfigInfo> {
    return this.post<DaemonConfigInfo>(
      '/api/daemon/config',
      {
        max_concurrent: input.maxConcurrent,
        agent_timeout: input.agentTimeout,
        work_dir: input.workDir,
        agent_cwd_allowlist: input.agentCwdAllowlist,
      },
      10_000,
    );
  }

  async runtimes(): Promise<RuntimeInfo[]> {
    const res = await this.request<{ runtimes: RuntimeInfo[] }>('/api/runtimes');
    return res.runtimes;
  }

  async consoleProjects(): Promise<ConsoleProjectInfo[]> {
    const res = await this.request<{ projects: ConsoleProjectInfo[] }>(
      '/api/console/projects',
    );
    return res.projects;
  }

  async consoleProject(projectId: string): Promise<ConsoleProjectInfo> {
    return this.request(
      `/api/console/projects/${encodeURIComponent(projectId)}`,
    );
  }

  async createConsoleProject(input: {
    path: string;
    name?: string | null;
  }): Promise<ConsoleProjectInfo> {
    return this.post(
      '/api/console/projects',
      { path: input.path, name: input.name || null },
      10_000,
    );
  }

  async renameConsoleProject(
    projectId: string,
    name: string,
  ): Promise<ConsoleProjectInfo> {
    return this.patch(
      `/api/console/projects/${encodeURIComponent(projectId)}`,
      { name },
      10_000,
    );
  }

  async deleteConsoleProject(projectId: string): Promise<void> {
    return this.delete(
      `/api/console/projects/${encodeURIComponent(projectId)}`,
      10_000,
    );
  }

  async openConsoleProject(projectId: string): Promise<ConsoleProjectInfo> {
    return this.post(
      `/api/console/projects/${encodeURIComponent(projectId)}/open`,
      {},
      10_000,
    );
  }

  async consoleDirectories(path?: string | null): Promise<ConsoleDirectoryListing> {
    const query = path ? `?${new URLSearchParams({ path })}` : '';
    return this.request(`/api/console/directories${query}`, {}, 10_000);
  }

  async pickConsoleDirectory(): Promise<string | null> {
    const result = await this.post<{ path: string | null }>(
      '/api/console/directories/pick',
      {},
      310_000,
    );
    return result.path;
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
    const res = await this.request<{ identities: IdentityWithBindings[] }>(
      '/api/identities/overview',
    );
    return res.identities;
  }

  async createIdentity(input: IdentityCreateInput): Promise<IdentityInfo> {
    return this.post<IdentityInfo>('/api/identities', input, 10_000);
  }

  async updateIdentity(did: string, input: IdentityUpdateInput): Promise<IdentityInfo> {
    return this.put<IdentityInfo>(
      `/api/identities/${encodeURIComponent(did)}`,
      input,
      10_000,
    );
  }

  async createIdentityBinding(
    did: string,
    input: {
      daemonId: string;
      provider: string;
      priority?: number;
      isDefault?: boolean;
    },
  ): Promise<IdentityBindingInfo> {
    return this.post<IdentityBindingInfo>(
      `/api/identities/${encodeURIComponent(did)}/bindings`,
      {
        daemon_id: input.daemonId,
        provider: input.provider,
        priority: input.priority ?? 0,
        is_default: input.isDefault ?? false,
      },
      10_000,
    );
  }

  async updateIdentityBinding(
    did: string,
    bindingId: number,
    input: { priority?: number; isDefault?: boolean },
  ): Promise<IdentityBindingInfo> {
    return this.patch<IdentityBindingInfo>(
      `/api/identities/${encodeURIComponent(did)}/bindings/${bindingId}`,
      {
        priority: input.priority,
        is_default: input.isDefault,
      },
      10_000,
    );
  }

  async deleteIdentityBinding(did: string, bindingId: number): Promise<void> {
    return this.delete(
      `/api/identities/${encodeURIComponent(did)}/bindings/${bindingId}`,
      10_000,
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

  /**
   * Submit a SOP for execution.
   *
   * ``spec`` is the **parsed document** (the server re-validates it with
   * `parse_sop`), matching what the CLI sends. ``cwd`` / ``localDaemonId``
   * let the server default an un-pinned node's ``agent_cwd``.
   */
  async submitSop(input: {
    spec: unknown;
    cwd?: string | null;
    localDaemonId?: string | null;
    templatePath?: string | null;
    templateHash?: string | null;
  }): Promise<SopSubmitResult> {
    return this.post<SopSubmitResult>('/api/sop/submit', {
      spec: input.spec,
      cwd: input.cwd ?? null,
      local_daemon_id: input.localDaemonId ?? null,
      template_path: input.templatePath ?? null,
      template_hash: input.templateHash ?? null,
    });
  }

  async sopStatus(rootJobId: string): Promise<SopStatus> {
    return this.request<SopStatus>(
      `/api/sop/${encodeURIComponent(rootJobId)}/status`,
    );
  }

  async sopNodeLog(rootJobId: string, nodeName: string): Promise<SopNodeLog> {
    return this.request<SopNodeLog>(
      `/api/sop/${encodeURIComponent(rootJobId)}/nodes/${encodeURIComponent(nodeName)}/log`,
    );
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

  async deleteSession(
    sessionId: string,
    input: {
      provider: string;
      daemonId: string;
      finalizeActive?: boolean;
    },
  ): Promise<{
    ok: boolean;
    session_id: string;
    session_rows_deleted: number;
    task_pointers_cleared: number;
  }> {
    return this.post(
      `/api/sessions/${encodeURIComponent(sessionId)}/delete`,
      {
        confirm: true,
        provider: input.provider,
        daemon_id: input.daemonId,
        finalize_active: input.finalizeActive ?? false,
      },
      10_000,
    );
  }

  /**
   * Store one chat attachment on the Server and get its ``agt-upload://``
   * reference. Base64 is used because the webview cannot post a Blob through
   * ``postMessage`` and the Server deliberately avoids multipart parsing.
   */
  async uploadInput(input: {
    name: string;
    contentB64: string;
    mimeType?: string | null;
    source?: string;
  }): Promise<InputUploadInfo> {
    return this.post<InputUploadInfo>(
      '/api/uploads',
      {
        name: input.name,
        mime_type: input.mimeType ?? null,
        content_b64: input.contentB64,
        source: input.source ?? 'console',
      },
      60_000,
    );
  }

  /** Drop an attachment the user removed before sending it (best effort). */
  async deleteInputUpload(uploadId: string): Promise<void> {
    return this.delete(`/api/uploads/${encodeURIComponent(uploadId)}`, 10_000);
  }

  async createChatJob(input: {
    prompt: string;
    provider: string;
    targetDaemonId: string;
    identityDid?: string | null;
    agentCwd?: string | null;
    message?: ChatInputMessage | null;
  }): Promise<JobInfo> {
    return this.post<JobInfo>('/api/jobs', {
      prompt: input.prompt,
      provider: input.provider,
      target_daemon_id: input.targetDaemonId,
      identity_did: input.identityDid ?? null,
      agent_cwd: input.agentCwd ?? null,
      message: input.message ?? null,
    });
  }

  async appendChatInput(
    jobId: string,
    prompt: string,
    message?: ChatInputMessage | null,
  ): Promise<JobInfo> {
    return this.post<JobInfo>(`/api/jobs/${encodeURIComponent(jobId)}/input`, {
      prompt,
      message: message ?? null,
    });
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

  async stopChatJob(jobId: string): Promise<{
    job_id: string;
    status: string;
    interrupted_task_ids: string[];
  }> {
    return this.post(`/api/jobs/${encodeURIComponent(jobId)}/stop`, {});
  }

  async submitGoal(input: {
    goal: string;
    identityDid?: string | null;
    provider?: string | null;
    daemonId?: string | null;
    dir?: string | null;
    cwd?: string | null;
    maxDepth?: number;
    maxDescendants?: number;
  }): Promise<GoalSubmitResult> {
    return this.post<GoalSubmitResult>('/api/goal/submit', {
      goal: input.goal,
      identity_did: input.identityDid ?? null,
      provider: input.provider ?? null,
      daemon_id: input.daemonId ?? null,
      dir: input.dir ?? null,
      cwd: input.cwd ?? null,
      max_depth: input.maxDepth ?? 5,
      max_descendants: input.maxDescendants ?? 64,
    });
  }

  async goalRuns(limit = 50, offset = 0): Promise<GoalRunListInfo> {
    return this.request<GoalRunListInfo>(
      `/api/goal/runs?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`,
    );
  }

  /**
   * Unified run history across chat, sop, and goal modes.
   *
   * ``cwd`` should be the project directory used to isolate runs. The server
   * returns one flat ``RunInfo`` per run regardless of mode.
   */
  async listRuns(input: {
    mode?: 'chat' | 'sop' | 'goal';
    cwd?: string | null;
    daemonId?: string | null;
    provider?: string | null;
    templatePath?: string | null;
    limit?: number;
    offset?: number;
  }): Promise<RunListInfo> {
    const params = new URLSearchParams({
      limit: String(input.limit ?? 50),
      offset: String(input.offset ?? 0),
    });
    if (input.mode) params.set('mode', input.mode);
    if (input.cwd) params.set('cwd', input.cwd);
    if (input.daemonId) params.set('daemon_id', input.daemonId);
    if (input.provider) params.set('provider', input.provider);
    if (input.templatePath) params.set('template_path', input.templatePath);
    return this.request<RunListInfo>(`/api/runs?${params}`);
  }

  async jobTree(jobId: string): Promise<{ root_job_id: string; goal: GoalRunInfo['meta'] | null; node: GoalJobTreeNode }> {
    return this.request(`/api/jobs/${encodeURIComponent(jobId)}/tree`);
  }

  async jobLog(jobId: string): Promise<JobLogInfo> {
    return this.request(`/api/jobs/${encodeURIComponent(jobId)}/log`);
  }

  async goalExport(rootJobId: string, includeFailed = false, keepPlanning = false): Promise<{ yaml: string; warnings: string[] }> {
    const params = new URLSearchParams({
      include_failed: String(includeFailed),
      keep_planning: String(keepPlanning),
    });
    return this.request(`/api/goal/${encodeURIComponent(rootJobId)}/export?${params}`);
  }

  async taskStatus(taskId: string): Promise<TaskStatusInfo> {
    return this.request<TaskStatusInfo>(`/api/tasks/${encodeURIComponent(taskId)}/status`);
  }

  async artifact(artifactId: string): Promise<ArtifactInfo> {
    return this.request<ArtifactInfo>(`/api/artifacts/${encodeURIComponent(artifactId)}`);
  }

  async artifactContent(artifactId: string): Promise<Uint8Array> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30_000);
    try {
      const res = await fetch(
        `${this.baseUrl}/api/artifacts/${encodeURIComponent(artifactId)}/content`,
        { signal: controller.signal, headers: { accept: '*/*' } },
      );
      if (!res.ok) {
        let detail = `HTTP ${res.status}`;
        try {
          detail = `${detail}: ${(await res.text()).slice(0, 240)}`;
        } catch {}
        throw new Error(detail);
      }
      return new Uint8Array(await res.arrayBuffer());
    } finally {
      clearTimeout(timer);
    }
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

  async consoleBootstrap(): Promise<{
    console_url: string;
    server_version: string;
    default_project: string;
    workspace_roots: string[];
    capabilities: Record<string, boolean>;
  }> {
    return this.request('/api/console/bootstrap', {}, 10_000);
  }

  async consoleSopTemplates(
    root: string,
  ): Promise<{ path: string; label: string; nodeCount: number }[]> {
    const res = await this.request<{
      templates: { path: string; label: string; nodeCount: number }[];
    }>(
      `/api/console/sop/templates?${new URLSearchParams({ root })}`,
      {},
      10_000,
    );
    return res.templates;
  }

  async readConsoleSopFile(path: string): Promise<{ path: string; text: string }> {
    return this.request(
      `/api/console/sop/file?${new URLSearchParams({ path })}`,
      {},
      10_000,
    );
  }

  async writeConsoleSopFile(path: string, text: string): Promise<{
    path: string;
    sha256: string;
    size: number;
  }> {
    return this.put(
      '/api/console/sop/file',
      { path, text },
      10_000,
    );
  }

  async renameConsoleSopFile(path: string, newName: string): Promise<{
    path: string;
    label: string;
  }> {
    return this.post(
      '/api/console/sop/file/rename',
      { path, new_name: newName },
      10_000,
    );
  }
}

async function errorMessage(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown; message?: unknown };
    const detail = body.detail ?? body.message;
    if (typeof detail === 'string' && detail.trim()) {
      return `HTTP ${res.status}: ${detail}`;
    }
    if (
      typeof detail === 'object'
      && detail !== null
      && 'message' in detail
      && typeof detail.message === 'string'
      && detail.message.trim()
    ) {
      return `HTTP ${res.status}: ${detail.message}`;
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
