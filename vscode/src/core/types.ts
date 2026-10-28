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

export type DaemonConfigValue = number | string | string[];

export interface DaemonConfigSetting {
  value: DaemonConfigValue;
  source: 'env' | 'file' | 'default' | string;
  saved: DaemonConfigValue | null;
  default: DaemonConfigValue | null;
  env_name: string | null;
}

export interface DaemonConfigInfo {
  config_file: string;
  settings: {
    max_concurrent: DaemonConfigSetting;
    agent_timeout: DaemonConfigSetting;
    work_dir: DaemonConfigSetting;
    agent_cwd_allowlist: DaemonConfigSetting;
  };
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

export interface ConsoleProjectRunCounts {
  chat: number;
  sop: number;
  goal: number;
}

export interface ConsoleProjectInfo {
  id: string;
  name: string;
  path: string;
  available: boolean;
  created_at: string;
  updated_at: string;
  last_opened_at: string | null;
  run_counts: ConsoleProjectRunCounts;
}

export interface ConsoleDirectoryInfo {
  name: string;
  path: string;
}

export interface ConsoleDirectoryListing {
  roots: string[];
  parent: string | null;
  current: string;
  directories: ConsoleDirectoryInfo[];
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
  runtime_online?: boolean;
  last_seen?: string | null;
}

export interface IdentityWithBindings extends IdentityInfo {
  bindings: IdentityBindingInfo[];
}

export interface IdentityCreateInput {
  daemon_id: string;
  name: string;
  slug?: string | null;
  description: string;
  capabilities: string[];
  visibility?: 'private' | 'public';
}

export interface IdentityUpdateInput {
  name?: string;
  description?: string;
  add_capabilities?: string[];
  remove_capabilities?: string[];
}

export interface ChatSessionInfo {
  session_id: string;
  task_id: string;
  provider: string;
  identity_did: string | null;
  dir: string | null;
  first_prompt: string;
  last_seen_at: string;
  /** Job root id used to resume/delete a chat run via the unified runs API. */
  job_id: string;
}

/** Unified run row returned by ``GET /api/runs``. */
export interface RunInfo {
  root_job_id: string;
  mode: 'chat' | 'sop' | 'goal';
  status: string;
  provider: string;
  identity_did: string | null;
  daemon_id: string | null;
  cwd: string | null;
  template_path: string | null;
  template_hash: string | null;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface RunListInfo {
  runs: RunInfo[];
  total: number;
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
  iteration: number;
  loop_name: string | null;
  depends_on: string[];
  artifacts: string[];
  artifact_details?: SopArtifactSummary[];
  provider: string | null;
  identity_did?: string | null;
  daemon_id?: string | null;
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
  iteration: number;
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

export interface ArtifactInfo {
  artifact_id: string;
  job_id: string;
  step_id: string;
  producer_identity_id: string | null;
  name: string;
  mime_type: string;
  summary: string | null;
  status_flag: string | null;
  size: number;
  sha256: string;
  status: string;
  uri: string;
  created_at: string;
}

export interface GoalBudgetInfo {
  max_depth: number;
  max_descendants: number;
  max_batch: number;
}

export interface GoalMetaInfo {
  goal: string;
  template: string | null;
  budget: GoalBudgetInfo | null;
  goal_sha256: string | null;
}

export interface GoalRunInfo {
  root_job_id: string;
  goal: string;
  status: string;
  provider: string;
  identity_did: string | null;
  daemon_id: string | null;
  cwd: string | null;
  created_at: string;
  updated_at: string;
  total_nodes: number;
  status_counts: Record<string, number>;
  meta: GoalMetaInfo;
}

export interface GoalRunListInfo {
  runs: GoalRunInfo[];
  total: number;
}

export interface GoalSubmitResult {
  root_job_id: string;
  status: string;
}

export interface GoalJobTreeNode {
  job_id: string;
  parent_job_id: string | null;
  status: string;
  provider: string;
  identity_did: string | null;
  daemon_id: string | null;
  session_id: string | null;
  label: string;
  duration: number | null;
  current_task_id: string | null;
  ask_question: string | null;
  artifacts: ArtifactInfo[];
  error: string | null;
  children: GoalJobTreeNode[];
}

export interface GoalTreeInfo {
  root_job_id: string;
  goal: GoalMetaInfo | null;
  node: GoalJobTreeNode;
}

export interface JobLogInfo {
  job_id: string;
  status: string;
  steps: SopNodeLogStep[];
}
