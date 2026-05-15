export type IdeaStatus = 'QUEUED' | 'RUNNING' | 'PAUSED' | 'CONVERGED' | 'ABANDONED' | 'SELECTED';
export type BranchStatus = 'QUEUED' | 'RUNNING' | 'PAUSED' | 'VIABLE' | 'FAILED' | 'CANCELLED';
export type StageStatus = 'PENDING' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'SKIPPED';

export interface BranchSummary {
  id: string;
  branch_index: number;
  status: BranchStatus;
  current_stage: number;
  approach_summary: string | null;
  parent_branch_id: string | null;
  failure_reason: string | null;
  created_at: string;
  updated_at: string;
}

export interface IdeaSummary {
  id: string;
  name: string;
  status: IdeaStatus;
  active_branch_count: number;
  viable_branch_count: number;
  phase: number;
  phase_label: string;
  parent_idea_id: string | null;
  parent_idea_name: string | null;
  created_at: string;
  updated_at: string;
}

export interface IdeaDetail {
  id: string;
  name: string;
  description: string;
  requirements: string;
  constraints: string;
  status: IdeaStatus;
  selected_branch_id: string | null;
  selected_at: string | null;
  selection_notes: string | null;
  created_at: string;
  updated_at: string;
  branches: BranchSummary[];
}

export interface StageResult {
  id: string;
  stage_index: number;
  stage_name: string;
  status: StageStatus;
  output_json: string | null;
  failed: boolean;
  failure_reason: string | null;
  started_at: string | null;
  completed_at: string | null;
}

export interface BranchDetail {
  id: string;
  idea_id: string;
  branch_index: number;
  status: BranchStatus;
  current_stage: number;
  approach_summary: string | null;
  parent_branch_id: string | null;
  failure_stage: number | null;
  failure_reason: string | null;
  created_at: string;
  updated_at: string;
  stage_results: StageResult[];
}

export interface DocumentMeta {
  id: string;
  doc_type: string;
  file_path: string;
  created_at: string;
}

export interface ModelCall {
  id: string;
  branch_id: string | null;
  stage_result_id: string | null;
  call_type: 'STAGE' | 'FAILURE_ANALYSIS' | 'PHASE2' | 'PHASE3' | 'SCRIPT_EXECUTION' | 'WEB_SEARCH' | 'FILE_EDIT' | 'SHELL_EXECUTION';
  call_index: number;
  model_name: string;
  backend: string;
  prompt_json: string;
  response_json: string;
  tokens_prompt: number | null;
  tokens_completion: number | null;
  duration_ms: number | null;
  created_at: string;
}

export interface FailureAnalysis {
  id: string;
  failed_branch_id: string;
  new_path_exists: boolean;
  suggested_direction: string | null;
  reasoning: string;
  spawned_branch_id: string | null;
  created_at: string;
}

export interface Phase2Message {
  id: string;
  session_id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
}

export interface Phase2Session {
  id: string;
  idea_id: string;
  branch_id: string;
  status: 'RESOLVING' | 'READY' | 'IMPLEMENTING' | 'COMPLETE';
  resolution_summary: string | null;
  created_at: string;
  updated_at: string;
  messages: Phase2Message[];
}

export interface PipelineEvent {
  event_type: string;
  idea_id: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export interface Phase3ActivityEvent {
  id: string;
  event_type: 'plan_ready' | 'pass_started' | 'file_written' | 'file_failed' | 'command_executed' | 'sub_agent_queued' | 'sub_agent_started' | 'sub_agent_complete' | 'error';
  payload: Record<string, unknown>;
  created_at: string;
}

export interface Phase3Session {
  id: string;
  idea_id: string;
  phase2_session_id: string;
  branch_id: string;
  implementation_type: string;
  status: 'PLANNING' | 'RUNNING' | 'WAITING' | 'COMPLETE' | 'FAILED';
  mode: 'classic' | 'multi_agent';
  project_root: string | null;
  output_dir: string | null;
  summary: string | null;
  created_at: string;
  updated_at: string;
}

export interface Phase3ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  created_at: string;
}

export interface Phase3DirEntry {
  path: string;
  size: number;
  type: 'file' | 'dir';
}

export interface Phase3DirList {
  entries: Phase3DirEntry[];
  output_dir: string | null;
}

export const STAGE_NAMES = [
  'Intake',
  'Feasibility',
  'Solution Design',
  'Solution Analysis',
  'Decomposition',
  'Component Validation',
  'Deep Review',
  'Documentation',
];

// ---------------------------------------------------------------------------
// Telemetry / Ops dashboard
// ---------------------------------------------------------------------------

export interface ModelStat {
  model: string;
  backend: string;
  calls: number;
  success: number;
  fallbacks: number;
  success_rate: number;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  tokens_prompt: number;
  tokens_completion: number;
  tokens_total: number;
}

export interface StageStat {
  stage: string;
  calls: number;
  success: number;
  fallbacks: number;
  success_rate: number;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  tokens_prompt: number;
  tokens_completion: number;
  tokens_total: number;
}

export interface ProjectStat {
  project_id: string;
  project_name: string;
  calls: number;
  success: number;
  fallbacks: number;
  success_rate: number;
  tokens_prompt: number;
  tokens_completion: number;
  tokens_total: number;
}

export interface BackendStat {
  backend: string;
  calls: number;
  success: number;
  fallbacks: number;
  success_rate: number;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  tokens_prompt: number;
  tokens_completion: number;
  tokens_total: number;
}

export interface TimeBucket {
  bucket: string;
  calls: number;
  success: number;
  avg_duration_ms: number | null;
  tokens_prompt: number;
  tokens_completion: number;
  tokens_total: number;
}

export interface ToolProjectStat {
  tool: string;
  avg_calls_per_project: number;
  projects_used: number;
}

export interface ToolModelStat {
  model: string;
  avg_tool_calls_per_invocation: number;
  invocations_with_tools: number;
}

export interface TaskTypeStat {
  model_type: string;
  calls: number;
  success: number;
  fallbacks: number;
  success_rate: number;
  avg_duration_ms: number | null;
  p95_duration_ms: number | null;
  avg_tool_calls: number | null;
}

export interface TypeProjectStat {
  model_type: string;
  avg_tasks_per_project: number;
  projects: number;
  total_tasks: number;
}

export interface ErrorCount {
  error: string;
  model: string;
  count: number;
}

export interface TelemetrySummary {
  total_calls: number;
  total_tokens_prompt: number;
  total_tokens_completion: number;
  total_tokens: number;
  period_hours: number;
  by_model: ModelStat[];
  by_stage: StageStat[];
  by_project: ProjectStat[];
  by_backend: BackendStat[];
  by_type: TaskTypeStat[];
  avg_tasks_per_project_by_type: TypeProjectStat[];
  over_time: TimeBucket[];
  avg_tools_per_project: ToolProjectStat[];
  avg_tools_per_model: ToolModelStat[];
  by_error: ErrorCount[];
  available_models: string[];
  available_backends: string[];
  available_stages: string[];
  available_projects: { id: string; name: string }[];
}

export interface TelemetryCall {
  ts: string;
  project_id: string;
  project_name: string;
  stage: string;
  model: string;
  backend: string;
  duration_ms: number | null;
  success: boolean;
  is_fallback: boolean;
  fallback_from: string | null;
  tokens_prompt: number | null;
  tokens_completion: number | null;
  error: string | null;
}

export interface TelemetryCallsResponse {
  calls: TelemetryCall[];
  total: number;
}
