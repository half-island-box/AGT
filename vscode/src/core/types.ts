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

export interface IdentityInfo {
  did: string;
  daemon_id: string;
  name: string;
  description: string;
  capabilities: string[];
  status: string;
  visibility: string;
  created_by: string | null;
  created_at: string;
  updated_at: string;
}

export interface IdentityBindingInfo {
  id: number;
  identity_did: string;
  daemon_id: string;
  provider: string;
  priority: number;
  is_default: boolean;
  created_at: string;
}

export interface IdentityWithBindings extends IdentityInfo {
  bindings: IdentityBindingInfo[];
}

export interface ChatSessionInfo {
  session_id: string;
  task_id: string;
  provider: string;
  identity_did: string | null;
  dir: string | null;
  first_prompt: string;
  last_seen_at: string;
}

export interface SessionResolveInfo {
  job_id: string;
  task_id: string;
  session_id: string;
  provider: string;
  identity_did: string | null;
  daemon_id: string | null;
  dir: string | null;
}

export interface SessionHistoryMessage {
  role: 'user' | 'assistant' | 'system' | string;
  content: string;
  timestamp?: number | null;
}

export interface SessionHistoryInfo {
  session_id: string;
  messages: SessionHistoryMessage[];
  not_implemented: boolean;
}

export interface JobInfo {
  job_id: string;
  task_id: string;
  status: string;
}

export interface JobStatusInfo {
  job_id: string;
  status: string;
  current_step_id: string | null;
}

export interface TaskStatusInfo {
  task_id: string;
  provider: string;
  status: string;
  session_id: string | null;
  exit_code: number | null;
  stdout: string | null;
  stderr: string | null;
  duration: number | null;
  created_at: string;
  updated_at: string;
}

export type TaskStreamEvent =
  | { type: 'snapshot'; status: string; stdout: string }
  | { type: 'delta'; chunk: string }
  | {
      type: 'done';
      status: string;
      stdout: string;
      session_id?: string | null;
      exit_code?: number | null;
      resume_rejected?: boolean | null;
      stderr?: string | null;
    };

export type ServerState = 'unknown' | 'offline' | 'starting' | 'online';

export type UiLanguage = 'zh' | 'en';

/** One DAG node as the server sees it (``POST /api/sop/validate``). */
export interface SopValidationNode {
  name: string;
  depends_on: string[];
  output: string | null;
  identity: string | null;
}

/**
 * Result of server-side SOP validation. Never a transport error: a failed
 * validation is ``ok: false`` plus a human-readable ``error``.
 */
export interface SopValidation {
  ok: boolean;
  name: string | null;
  nodes: SopValidationNode[];
  identities: string[];
  error: string | null;
}

/** ``POST /api/sop/submit`` — one Job per DAG node, keyed by node name. */
export interface SopSubmitResult {
  root_job_id: string;
  nodes: Record<string, string>;
}

/** One node as reported by ``GET /api/sop/{root}/status``. */
export interface SopNodeStatus {
  name: string;
  job_id: string;
  status: string;
  depends_on: string[];
  artifacts: string[];
  artifact_details?: SopArtifactSummary[];
  provider: string | null;
  session_id: string | null;
}

export interface SopArtifactSummary {
  uri: string;
  name: string;
}

export interface SopStatus {
  root_job_id: string;
  nodes: SopNodeStatus[];
  all_terminal: boolean;
}

export interface SopNodeLogStep {
  task_id: string;
  status: string;
  session_id: string | null;
  exit_code: number | null;
  stdout: string | null;
  stderr: string | null;
}

export interface SopNodeLog {
  node_name: string;
  job_id: string;
  status: string;
  provider: string | null;
  steps: SopNodeLogStep[];
}
