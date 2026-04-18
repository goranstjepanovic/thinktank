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
  event_type: 'plan_ready' | 'pass_started' | 'file_written' | 'file_failed' | 'command_executed' | 'error';
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

export interface Phase3FileEntry {
  path: string;
  size: number;
}

export interface Phase3FileList {
  files: Phase3FileEntry[];
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
