/* ── Database row types (match SQLite schema from db.py) ─────────── */

export interface User {
  username: string;
  display_name: string | null;
  bio: string | null;
  avatar_url: string | null;
  profile_visibility: "private" | "unlisted" | "public";
  default_session_visibility: "private" | "unlisted" | "public";
  created_at: string;
  updated_at: string;
}

export interface Session {
  session_id: string;
  source: "claudecode" | "codex";
  username: string;
  machine: string | null;
  project: string | null;
  /* repo metadata */
  workspace_root: string | null;
  worktree_root: string | null;
  repo_root: string | null;
  repo_name: string | null;
  git_branch: string | null;
  git_commit: string | null;
  git_dirty: number;
  /* paths */
  source_path: string;
  shared_path: string;
  /* timestamps */
  first_timestamp: string | null;
  last_timestamp: string | null;
  duration_seconds: number | null;
  /* message counts */
  user_message_count: number;
  assistant_message_count: number;
  tool_call_count: number;
  error_count: number;
  /* deterministic activity counts */
  write_path_count: number | null;
  read_path_count: number | null;
  search_path_count: number | null;
  test_run_count: number | null;
  test_failure_count: number | null;
  lint_run_count: number | null;
  lint_failure_count: number | null;
  build_run_count: number | null;
  build_failure_count: number | null;
  format_run_count: number | null;
  format_failure_count: number | null;
  git_status_count: number | null;
  git_diff_count: number | null;
  git_commit_count: number | null;
  session_goal: string | null;
  session_summary: string | null;
  session_outcome: string | null;
  session_status: "exploration" | "success" | "partial" | "failed" | null;
  objective_family: string | null;
  objective_label: string | null;
  session_origin: "human_direct" | "human_delegated" | "system_generated" | "pipeline_eval" | "meta_scaffolding" | null;
  /* tokens */
  total_input_tokens: number;
  total_output_tokens: number;
  first_user_message: string | null;
  visibility: "private" | "unlisted" | "public";
  is_private: number;
  file_hash: string | null;
  synced_at: string | null;
  model: string | null;
}

export interface ToolCall {
  id: number;
  session_id: string;
  tool_name: string;
  command: string | null;
  timestamp: string | null;
  is_error: number;
}

/* ── Derived / view types ────────────────────────────────────────── */

export interface DashboardStats {
  total_sessions: number;
  total_user_msgs: number;
  total_assistant_msgs: number;
  total_tool_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  active_users: number;
  total_projects: number;
}

export interface SessionRow extends Session {
  user_display_name: string;
  tokens: number;
}

export interface UserListRow {
  username: string;
  display_name: string;
  bio: string | null;
  sessions: number;
  messages: number;
  tool_calls: number;
  tokens: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface UserSummary {
  total_sessions: number;
  total_messages: number;
  total_tool_calls: number;
  total_tokens: number;
  active_days: number;
  known_projects: number;
  known_repos: number;
  write_paths: number;
  test_runs: number;
  test_failures: number;
  build_runs: number;
  build_failures: number;
  git_commits: number;
  success_sessions: number;
  partial_sessions: number;
  failed_sessions: number;
  exploration_sessions: number;
  first_seen: string | null;
  last_seen: string | null;
}

export interface RepoRow {
  repo_name: string;
  repo_root: string | null;
  sessions: number;
  worktrees: number;
  branches: number;
  messages: number;
  tool_calls: number;
  unique_paths: number;
  last_seen: string | null;
}

export interface RunawaySessionRow {
  session_id: string;
  source: "claudecode" | "codex";
  username: string;
  user_display_name: string;
  project: string | null;
  repo_name: string | null;
  session_status: SessionStatus | null;
  session_summary: string | null;
  first_timestamp: string | null;
  duration_seconds: number | null;
  tool_call_count: number;
  error_count: number;
}

export interface ObjectiveRelaunchRow {
  objective_key: string;
  display_label: string;
  launches: number;
  operator_count: number;
  total_tool_calls: number;
  total_errors: number;
  latest_timestamp: string | null;
  latest_status: SessionStatus | null;
  latest_session_id: string;
  latest_repo_name: string | null;
  latest_summary: string | null;
}

export interface ContextExplosionChildRow {
  session_id: string;
  agent_name: string | null;
  agent_role: string | null;
  total_tokens: number;
  total_input_tokens: number;
  cached_input_tokens: number;
  total_output_tokens: number;
  tool_call_count: number;
  error_count: number;
  spawn_depth: number;
  first_timestamp: string | null;
  is_root: boolean;
}

export interface ContextExplosionWorkstreamRow {
  root_session_id: string;
  username: string;
  user_display_name: string;
  repo_name: string | null;
  project: string | null;
  display_label: string;
  root_summary: string | null;
  root_first_timestamp: string | null;
  total_tokens: number;
  total_input_tokens: number;
  fresh_input_tokens: number;
  cached_input_tokens: number;
  total_output_tokens: number;
  session_count: number;
  child_session_count: number;
  max_spawn_depth: number;
  top_child_tokens: number;
  child_token_share: number;
  cached_input_share: number;
  warnings: string[];
  top_children: ContextExplosionChildRow[];
}

export interface ChartDataset {
  label: string;
  data: number[];
  borderColor?: string;
  backgroundColor?: string;
  tension?: number;
  fill?: boolean;
  borderWidth?: number;
  pointRadius?: number;
  borderRadius?: number;
}

export interface ChartData {
  labels: string[];
  datasets: ChartDataset[];
}

/* ── Activity filter type ────────────────────────────────────────── */

export const ACTIVITY_FILTERS = [
  "write", "read", "search",
  "test", "test_failed",
  "lint", "lint_failed",
  "build", "build_failed",
  "format", "format_failed",
  "git_status", "git_diff", "git_commit",
  "error",
] as const;

export type ActivityFilter = typeof ACTIVITY_FILTERS[number];

/* ── Session status ──────────────────────────────────────────────── */

export type SessionStatus = "exploration" | "success" | "partial" | "failed";
export type SessionOrigin =
  | "human_direct"
  | "human_delegated"
  | "system_generated"
  | "pipeline_eval"
  | "meta_scaffolding";

export const SESSION_STATUSES: SessionStatus[] = [
  "exploration", "success", "partial", "failed",
];

export const SESSION_ORIGINS: SessionOrigin[] = [
  "human_direct",
  "human_delegated",
  "pipeline_eval",
  "meta_scaffolding",
  "system_generated",
];

/* ── Publish types ───────────────────────────────────────────────── */

export interface PublishCandidate {
  session_id: string;
  source: string;
  username: string;
  display_name: string;
  session_origin?: SessionOrigin | null;
  project: string | null;
  repo_name: string | null;
  visibility: "private" | "unlisted" | "public";
  first_timestamp: string | null;
  last_timestamp: string | null;
  session_status: SessionStatus | null;
  session_goal: string | null;
  session_summary: string | null;
  session_outcome: string | null;
  review_recommendation?: "private" | "unlisted" | "public";
  review_rationale?: string;
  needs_visibility_change?: boolean;
  finding_count: number;
  high_findings: number;
  medium_findings: number;
}

export interface PublishFinding {
  category: "secret" | "pii" | "missing";
  severity: "high" | "medium";
  title: string;
  evidence: string;
  source: string;
  line_number: number | null;
}

export interface PublishReview {
  session_id: string;
  source: string;
  current_visibility: "private" | "unlisted" | "public";
  visibility_source: string;
  recommendation: "private" | "unlisted" | "public";
  rationale: string;
  inspected_path: string | null;
  metadata: Record<string, string | number | null>;
  findings: PublishFinding[];
}

export interface PublishQueueResponse {
  total: number;
  limit: number;
  visibility: string;
  status: string | null;
  origin?: SessionOrigin | null;
  reviews: boolean;
  candidates: PublishCandidate[];
}
