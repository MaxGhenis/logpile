import Database from "better-sqlite3";
import { config } from "./config";
import type {
  ActivityFilter,
  ContextExplosionWorkstreamRow,
  DashboardStats,
  ObjectiveRelaunchRow,
  RepoRow,
  RunawaySessionRow,
  Session,
  SessionOrigin,
  SessionRow,
  User,
  UserListRow,
  UserSummary,
} from "./types";

let _db: Database.Database | null = null;

/** Get (or create) the singleton SQLite connection. Read-only. */
export function getDb(): Database.Database {
  if (!_db) {
    _db = new Database(config.dbPath, { readonly: true });
    _db.pragma("journal_mode = WAL");
    _db.function("objective_family", (sessionGoal: string | null, firstUserMessage: string | null, sessionSummary: string | null) => {
      return normalizeObjectiveFamily(
        objectiveSeedText({
          session_goal: sessionGoal,
          first_user_message: firstUserMessage,
          session_summary: sessionSummary,
        })
      ) || "";
    });
  }
  return _db;
}

/* ── Visibility clauses ───────────────────────────────────────────── */

/** Only public profiles appear on global surfaces in public mode. */
function listedProfileClause(alias = "u"): string {
  if (!config.publicMode) {
    return `${alias}.listed_private = 1`;
  }
  return `${alias}.listed_public = 1`;
}

/** For list pages: show public sessions only in public mode, public+unlisted in private mode. */
function listedSessionClause(sessionAlias = "s", userAlias = "u"): string {
  void userAlias;
  if (config.publicMode) {
    return `${sessionAlias}.listed_public = 1`;
  }
  return `${sessionAlias}.listed_private = 1`;
}

/** Profile pages only include public sessions in public mode, public+unlisted in private mode. */
function profileSessionClause(alias = "s"): string {
  if (config.publicMode) {
    return `${alias}.direct_public = 1`;
  }
  return `${alias}.direct_private = 1`;
}

export function isProfileDirectlyVisible(user: User | undefined): boolean {
  if (!user) return false;
  if (!config.publicMode) return true;
  const row = user as User & { direct_public?: number };
  return row.direct_public === 1 || user.profile_visibility === "public" || user.profile_visibility === "unlisted";
}

/* ── Dashboard ────────────────────────────────────────────────────── */

export function getDashboardStats(origin?: string): DashboardStats {
  const db = getDb();
  const clauses = [listedSessionClause("s")];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        COUNT(*)                               AS total_sessions,
        SUM(user_message_count)                AS total_user_msgs,
        SUM(assistant_message_count)           AS total_assistant_msgs,
        SUM(tool_call_count)                   AS total_tool_calls,
        SUM(total_input_tokens)                AS total_input_tokens,
        SUM(total_output_tokens)               AS total_output_tokens,
        COUNT(DISTINCT username)              AS active_users,
        COUNT(DISTINCT project)                AS total_projects
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}`
    )
    .get(...params) as DashboardStats;
}

export function getRecentSessions(limit = 10, origin?: string): SessionRow[] {
  const db = getDb();
  const clauses = [listedSessionClause("s")];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.session_id, s.source, s.username, s.username,
        s.user_display_name,
        s.project, s.repo_name,
        s.session_goal, s.session_summary, s.session_outcome, s.session_status,
        s.first_timestamp,
        s.user_message_count, s.assistant_message_count,
        s.total_input_tokens + s.total_output_tokens AS tokens,
        s.first_user_message, s.duration_seconds, s.model
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, limit) as SessionRow[];
}

/* ── Chart data ───────────────────────────────────────────────────── */

// Day-bucketed charts read session_daily_effective: usage lands on the UTC
// day its events happened, not on the session's start date (sessions can
// span weeks; not-yet-resynced sessions degrade to start-date attribution).
export function getMessagesPerDay(days = 30, origin?: string) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  const clauses = [listedSessionClause("s", "u"), "d.day >= ?"];
  const params: unknown[] = [cutoff];
  appendOriginClause(clauses, params, origin);
  const rows = db
    .prepare(
      `SELECT
        d.day AS day,
        s.username AS user_key,
        s.username,
        s.user_display_name,
        SUM(d.user_message_count + d.assistant_message_count) AS msgs
      FROM session_daily_effective d
      JOIN session_catalog s ON s.session_id = d.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY day, user_key, s.username, s.user_display_name
      ORDER BY day`
    )
    .all(...params) as {
      day: string;
      user_key: string;
      username: string;
      user_display_name: string;
      msgs: number;
    }[];
  return rows;
}

export function getMessagesByTool(days = 30, origin?: string) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  const clauses = [listedSessionClause("s", "u"), "d.day >= ?"];
  const params: unknown[] = [cutoff];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        d.day AS day,
        s.source AS source,
        SUM(d.user_message_count + d.assistant_message_count) AS msgs
      FROM session_daily_effective d
      JOIN session_catalog s ON s.session_id = d.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY day, source
      ORDER BY day`
    )
    .all(...params) as { day: string; source: string; msgs: number }[];
}

export function getTopTools(limit = 20, origin?: string) {
  const db = getDb();
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT tc.tool_name, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY tc.tool_name
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(...params, limit) as { tool_name: string; cnt: number }[];
}

export function getErrorRate(limit = 15, origin?: string) {
  const db = getDb();
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.username AS user_key,
        s.username,
        s.user_display_name,
        SUM(s.error_count) AS errors,
        SUM(s.tool_call_count) AS tools
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      GROUP BY user_key, s.username, s.user_display_name
      ORDER BY errors DESC
      LIMIT ?`
    )
    .all(...params, limit) as {
      user_key: string;
      username: string;
      user_display_name: string;
      errors: number;
      tools: number;
    }[];
}

/* ── Sessions ─────────────────────────────────────────────────────── */

/** Map activity filter names to SQL WHERE clauses */
const ACTIVITY_SQL: Record<string, string> = {
  write: "s.write_path_count > 0",
  read: "s.read_path_count > 0",
  search: "s.search_path_count > 0",
  test: "s.test_run_count > 0",
  test_failed: "s.test_failure_count > 0",
  lint: "s.lint_run_count > 0",
  lint_failed: "s.lint_failure_count > 0",
  build: "s.build_run_count > 0",
  build_failed: "s.build_failure_count > 0",
  format: "s.format_run_count > 0",
  format_failed: "s.format_failure_count > 0",
  git_status: "s.git_status_count > 0",
  git_diff: "s.git_diff_count > 0",
  git_commit: "s.git_commit_count > 0",
  error: "s.error_count > 0",
};

function normalizeActivityFilter(activity?: string): ActivityFilter | undefined {
  if (!activity) {
    return undefined;
  }
  if (Object.hasOwn(ACTIVITY_SQL, activity)) {
    return activity as ActivityFilter;
  }
  throw new RangeError(`Invalid activity filter: ${activity}`);
}

function normalizeSessionStatus(status?: string): Session["session_status"] | undefined {
  if (!status) {
    return undefined;
  }
  if (status === "exploration" || status === "success" || status === "partial" || status === "failed") {
    return status;
  }
  throw new RangeError(`Invalid session status: ${status}`);
}

function normalizeSessionOrigin(origin?: string): SessionOrigin | undefined {
  if (!origin) {
    return undefined;
  }
  if (origin === "all") {
    return undefined;
  }
  if (
    origin === "human_direct" ||
    origin === "human_delegated" ||
    origin === "system_generated" ||
    origin === "pipeline_eval" ||
    origin === "meta_scaffolding"
  ) {
    return origin;
  }
  throw new RangeError(`Invalid session origin: ${origin}`);
}

function appendOriginClause(clauses: string[], params: unknown[], origin?: string, alias = "s"): void {
  const normalizedOrigin = normalizeSessionOrigin(origin);
  if (!normalizedOrigin) {
    return;
  }
  clauses.push(`COALESCE(${alias}.session_origin, 'human_direct') = ?`);
  params.push(normalizedOrigin);
}

function objectiveFamilySql(alias = "s"): string {
  return `COALESCE(NULLIF(${alias}.objective_family, ''), objective_family(${alias}.session_goal, ${alias}.first_user_message, ${alias}.session_summary), '')`;
}

export function getSessions(opts: {
  q?: string;
  objective?: string;
  source?: string;
  user?: string;
  repo?: string;
  repoRoot?: string;
  branch?: string;
  activity?: string;
  status?: string;
  origin?: string;
  page?: number;
  perPage?: number;
}) {
  const db = getDb();
  const {
    q,
    objective,
    source,
    user,
    repo,
    repoRoot,
    branch,
    activity,
    status,
    origin,
    page = 1,
    perPage = 50,
  } = opts;
  const normalizedActivity = normalizeActivityFilter(activity);
  const normalizedStatus = normalizeSessionStatus(status);
  const normalizedOrigin = normalizeSessionOrigin(origin);
  const normalizedObjective = normalizeObjectiveQuery(objective);
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];

  if (q) {
    clauses.push("s.first_user_message LIKE ?");
    params.push(`%${q}%`);
  }
  if (normalizedObjective) {
    clauses.push(`${objectiveFamilySql("s")} = ?`);
    params.push(normalizedObjective);
  }
  if (source) {
    clauses.push("s.source = ?");
    params.push(source);
  }
  if (user) {
    clauses.push("(s.username = ? OR s.username = ?)");
    params.push(user, user);
  }
  if (repo) {
    clauses.push("s.repo_name = ?");
    params.push(repo);
  }
  if (repoRoot) {
    clauses.push("s.repo_root = ?");
    params.push(repoRoot);
  }
  if (branch) {
    clauses.push("s.git_branch = ?");
    params.push(branch);
  }
  if (normalizedActivity) {
    clauses.push(ACTIVITY_SQL[normalizedActivity]);
  }
  if (normalizedStatus) {
    clauses.push("COALESCE(s.session_status, 'exploration') = ?");
    params.push(normalizedStatus);
  }
  if (normalizedOrigin) {
    clauses.push("COALESCE(s.session_origin, 'human_direct') = ?");
    params.push(normalizedOrigin);
  }

  const where = clauses.join(" AND ");
  const total = (
    db.prepare(
      `SELECT COUNT(*) AS c
      FROM session_catalog s
      WHERE ${where}`
    ).get(...params) as { c: number }
  ).c;

  const rows = db
    .prepare(
      `SELECT
        s.session_id, s.source, s.username, s.username, s.project,
        s.repo_name, ${config.publicMode ? "NULL" : "s.repo_root"} AS repo_root, s.git_branch,
        s.user_display_name AS user_display_name,
        s.session_goal, s.session_summary, s.session_outcome, s.session_status,
        s.objective_family, s.objective_label,
        s.session_origin,
        s.first_timestamp, s.last_timestamp, s.duration_seconds,
        s.user_message_count, s.assistant_message_count,
        s.tool_call_count, s.error_count,
        s.write_path_count, s.read_path_count,
        s.test_run_count, s.test_failure_count,
        s.build_run_count, s.build_failure_count,
        s.git_commit_count,
        s.total_input_tokens + s.total_output_tokens AS tokens,
        s.first_user_message, s.model
      FROM session_catalog s
      LEFT JOIN user_catalog u ON u.username = s.username
      WHERE ${where}
      ORDER BY s.first_timestamp DESC
      LIMIT ? OFFSET ?`
    )
    .all(...params, perPage, (page - 1) * perPage) as SessionRow[];

  return { rows, total, page, perPage };
}

export function getUsersForFilter() {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        u.username,
        COALESCE(u.display_name, u.username) AS display_name
      FROM user_catalog u
      WHERE ${listedProfileClause("u")}
        AND EXISTS (
          SELECT 1
          FROM session_catalog s
          WHERE s.username = u.username AND ${listedSessionClause("s")}
        )
      ORDER BY display_name`
    )
    .all() as { username: string; display_name: string }[];
}

/* ── Session detail ───────────────────────────────────────────────── */

export function getSession(sessionId: string) {
  const db = getDb();
  return db
    .prepare(
      `SELECT s.*,
        COALESCE(u.display_name, s.username) AS user_display_name,
        COALESCE(u.profile_visibility, 'public') AS user_profile_visibility
      FROM session_catalog s
      LEFT JOIN user_catalog u ON u.username = s.username
      WHERE s.session_id = ?`
    )
    .get(sessionId) as (SessionRow & { user_profile_visibility: string }) | undefined;
}

export function getSessionToolCalls(sessionId: string) {
  const db = getDb();
  return db
    .prepare(
      `SELECT tool_name, command, is_error
      FROM tool_calls WHERE session_id = ?`
    )
    .all(sessionId) as { tool_name: string; command: string | null; is_error: number }[];
}

/* ── Users ────────────────────────────────────────────────────────── */

export function getUsers(): UserListRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        u.username,
        u.username,
        COALESCE(u.display_name, u.username) AS display_name,
        u.bio,
        COUNT(s.session_id) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages,
        SUM(s.tool_call_count) AS tool_calls,
        SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
        MIN(s.first_timestamp) AS first_seen,
        MAX(s.last_timestamp) AS last_seen
      FROM user_catalog u
      LEFT JOIN session_catalog s ON s.username = u.username AND ${listedSessionClause("s", "u")}
      WHERE ${listedProfileClause("u")}
      GROUP BY u.username, u.display_name, u.bio
      ORDER BY last_seen DESC`
    )
    .all() as UserListRow[];
}

export function getApiSessions(limit = 500) {
  const db = getDb();
  const clampedLimit = Math.min(Math.max(limit, 1), 1000);
  return db
    .prepare(
      `SELECT
        s.session_id,
        s.source,
        s.username,
        s.username,
        s.user_display_name,
        s.project,
        s.repo_name,
        ${config.publicMode ? "NULL" : "s.workspace_root"} AS workspace_root,
        ${config.publicMode ? "NULL" : "s.worktree_root"} AS worktree_root,
        ${config.publicMode ? "NULL" : "s.repo_root"} AS repo_root,
        ${config.publicMode ? "NULL" : "s.git_branch"} AS git_branch,
        ${config.publicMode ? "NULL" : "s.git_commit"} AS git_commit,
        ${config.publicMode ? "NULL" : "s.git_dirty"} AS git_dirty,
        s.visibility,
        s.first_timestamp,
        s.user_message_count,
        s.assistant_message_count,
        s.tool_call_count,
        s.error_count,
        s.write_path_count,
        s.read_path_count,
        s.search_path_count,
        s.test_run_count,
        s.test_failure_count,
        s.lint_run_count,
        s.lint_failure_count,
        s.build_run_count,
        s.build_failure_count,
        s.format_run_count,
        s.format_failure_count,
        s.git_status_count,
        s.git_diff_count,
        s.git_commit_count,
        s.total_input_tokens,
        s.total_output_tokens
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")}
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(clampedLimit);
}

export function getApiSessionsFiltered(opts: {
  source?: string;
  project?: string;
  repo?: string;
  repoRoot?: string;
  branch?: string;
  activity?: string;
  status?: string;
  origin?: string;
  objective?: string;
  user?: string;
  path?: string;
  limit?: number;
}) {
  const db = getDb();
  const normalizedActivity = normalizeActivityFilter(opts.activity);
  const normalizedStatus = normalizeSessionStatus(opts.status);
  const normalizedOrigin = normalizeSessionOrigin(opts.origin);
  const normalizedObjective = normalizeObjectiveQuery(opts.objective);
  const clampedLimit = Math.min(Math.max(opts.limit ?? 500, 1), 1000);
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];

  if (opts.source) {
    clauses.push("s.source = ?");
    params.push(opts.source);
  }
  if (opts.project) {
    clauses.push("s.project = ?");
    params.push(opts.project);
  }
  if (opts.repo) {
    clauses.push("COALESCE(s.repo_name, '') = ?");
    params.push(opts.repo);
  }
  if (opts.repoRoot) {
    clauses.push("COALESCE(s.repo_root, '') = ?");
    params.push(opts.repoRoot);
  }
  if (opts.branch) {
    clauses.push("COALESCE(s.git_branch, '') = ?");
    params.push(opts.branch);
  }
  if (normalizedActivity) {
    clauses.push(ACTIVITY_SQL[normalizedActivity]);
  }
  if (normalizedStatus) {
    clauses.push("COALESCE(s.session_status, 'exploration') = ?");
    params.push(normalizedStatus);
  }
  if (normalizedObjective) {
    clauses.push(`${objectiveFamilySql("s")} = ?`);
    params.push(normalizedObjective);
  }
  if (normalizedOrigin) {
    clauses.push("COALESCE(s.session_origin, 'human_direct') = ?");
    params.push(normalizedOrigin);
  }
  if (opts.user) {
    clauses.push("(s.username = ? OR s.username = ?)");
    params.push(opts.user, opts.user);
  }
  if (opts.path) {
    clauses.push(
      `EXISTS (
        SELECT 1
        FROM session_paths sp
        WHERE sp.session_id = s.session_id
          AND (
            sp.display_path LIKE ?
            OR COALESCE(sp.relative_path, '') LIKE ?
            OR COALESCE(sp.repo_relative_path, '') LIKE ?
          )
      )`
    );
    params.push(`%${opts.path}%`, `%${opts.path}%`, `%${opts.path}%`);
  }

  return db
    .prepare(
      `SELECT
        s.session_id,
        s.source,
        s.username,
        s.username,
        s.user_display_name,
        s.project,
        s.repo_name,
        s.session_goal,
        s.session_summary,
        s.session_outcome,
        s.session_status,
        s.objective_family,
        s.objective_label,
        s.session_origin,
        ${config.publicMode ? "NULL" : "s.workspace_root"} AS workspace_root,
        ${config.publicMode ? "NULL" : "s.worktree_root"} AS worktree_root,
        ${config.publicMode ? "NULL" : "s.repo_root"} AS repo_root,
        ${config.publicMode ? "NULL" : "s.git_branch"} AS git_branch,
        ${config.publicMode ? "NULL" : "s.git_commit"} AS git_commit,
        ${config.publicMode ? "NULL" : "s.git_dirty"} AS git_dirty,
        s.visibility,
        s.first_timestamp,
        s.user_message_count,
        s.assistant_message_count,
        s.tool_call_count,
        s.error_count,
        s.write_path_count,
        s.read_path_count,
        s.search_path_count,
        s.test_run_count,
        s.test_failure_count,
        s.lint_run_count,
        s.lint_failure_count,
        s.build_run_count,
        s.build_failure_count,
        s.format_run_count,
        s.format_failure_count,
        s.git_status_count,
        s.git_diff_count,
        s.git_commit_count,
        s.total_input_tokens,
        s.total_output_tokens
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, clampedLimit);
}

export function getApiUsers() {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        u.username,
        u.username,
        COALESCE(u.display_name, u.username) AS display_name,
        u.bio,
        u.avatar_url,
        u.profile_visibility,
        COUNT(s.session_id) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages,
        SUM(s.tool_call_count) AS tool_calls,
        SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
        MAX(s.last_timestamp) AS last_seen
      FROM user_catalog u
      LEFT JOIN session_catalog s ON s.username = u.username AND ${listedSessionClause("s", "u")}
      WHERE ${listedProfileClause("u")}
      GROUP BY u.username, u.display_name, u.bio, u.avatar_url, u.profile_visibility
      ORDER BY last_seen DESC, sessions DESC, u.username`
    )
    .all() as Array<{
      username: string;
      display_name: string;
      bio: string | null;
      avatar_url: string | null;
      profile_visibility: string;
      sessions: number;
      messages: number;
      tool_calls: number;
      tokens: number;
      last_seen: string | null;
    }>;
}

export function getApiUserProfile(username: string, origin?: string) {
  const user = getUserByUsername(username);
  if (!user || !isProfileDirectlyVisible(user)) {
    return null;
  }

  const summary = getUserSummary(user.username, origin);
  return { user, summary };
}

export function getApiUserSessions(
  username: string,
  {
    limit = 50,
    offset = 0,
    project,
    repo,
    repoRoot,
    branch,
    activity,
    status,
    origin,
    objective,
    path,
  }: {
    limit?: number;
    offset?: number;
    project?: string;
    repo?: string;
    repoRoot?: string;
    branch?: string;
    activity?: string;
    status?: string;
    origin?: string;
    objective?: string;
    path?: string;
  } = {}
) {
  const user = getUserByUsername(username);
  if (!user || !isProfileDirectlyVisible(user)) {
    return null;
  }

  const db = getDb();
  const normalizedActivity = normalizeActivityFilter(activity);
  const normalizedStatus = normalizeSessionStatus(status);
  const normalizedOrigin = normalizeSessionOrigin(origin);
  const normalizedObjective = normalizeObjectiveQuery(objective);
  const clampedLimit = Math.min(Math.max(limit, 1), 200);
  const clampedOffset = Math.max(offset, 0);
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [user.username];

  if (project) {
    clauses.push("s.project = ?");
    params.push(project);
  }
  if (repo) {
    clauses.push("COALESCE(s.repo_name, '') = ?");
    params.push(repo);
  }
  if (repoRoot) {
    clauses.push("COALESCE(s.repo_root, '') = ?");
    params.push(repoRoot);
  }
  if (branch) {
    clauses.push("COALESCE(s.git_branch, '') = ?");
    params.push(branch);
  }
  if (normalizedActivity) {
    clauses.push(ACTIVITY_SQL[normalizedActivity]);
  }
  if (normalizedStatus) {
    clauses.push("COALESCE(s.session_status, 'exploration') = ?");
    params.push(normalizedStatus);
  }
  if (normalizedObjective) {
    clauses.push(`${objectiveFamilySql("s")} = ?`);
    params.push(normalizedObjective);
  }
  if (normalizedOrigin) {
    clauses.push("COALESCE(s.session_origin, 'human_direct') = ?");
    params.push(normalizedOrigin);
  }
  if (path) {
    clauses.push(
      `EXISTS (
        SELECT 1
        FROM session_paths sp
        WHERE sp.session_id = s.session_id
          AND (
            sp.display_path LIKE ?
            OR COALESCE(sp.relative_path, '') LIKE ?
            OR COALESCE(sp.repo_relative_path, '') LIKE ?
          )
      )`
    );
    params.push(`%${path}%`, `%${path}%`, `%${path}%`);
  }

  const total = (
    db.prepare(
      `SELECT COUNT(*) AS c
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}`
    ).get(...params) as { c: number }
  ).c;
  const sessions = db
    .prepare(
      `SELECT
        session_id,
        source,
        project,
        repo_name,
        model,
                session_goal,
                session_summary,
                session_outcome,
                session_status,
                objective_family,
                objective_label,
                session_origin,
        ${config.publicMode ? "NULL" : "workspace_root"} AS workspace_root,
        ${config.publicMode ? "NULL" : "worktree_root"} AS worktree_root,
        ${config.publicMode ? "NULL" : "repo_root"} AS repo_root,
        ${config.publicMode ? "NULL" : "git_branch"} AS git_branch,
        ${config.publicMode ? "NULL" : "git_commit"} AS git_commit,
        ${config.publicMode ? "NULL" : "git_dirty"} AS git_dirty,
        visibility,
        first_timestamp,
        last_timestamp,
        duration_seconds,
        user_message_count,
        assistant_message_count,
        tool_call_count,
        error_count,
        write_path_count,
        read_path_count,
        search_path_count,
        test_run_count,
        test_failure_count,
        lint_run_count,
        lint_failure_count,
        build_run_count,
        build_failure_count,
        format_run_count,
        format_failure_count,
        git_status_count,
        git_diff_count,
        git_commit_count,
        total_input_tokens,
        total_output_tokens
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY first_timestamp DESC
      LIMIT ? OFFSET ?`
    )
    .all(...params, clampedLimit, clampedOffset);

  return {
    user,
    total,
    limit: clampedLimit,
    offset: clampedOffset,
    sessions,
  };
}

export function getApiUserRules(username: string) {
  if (config.publicMode) {
    return null;
  }

  const user = getUserByUsername(username);
  if (!user) {
    return null;
  }

  const db = getDb();
  const rules = db
    .prepare(
      `SELECT
        id,
        source_scope,
        field,
        match_mode,
        pattern,
        visibility,
        priority,
        threshold,
        enabled
      FROM session_visibility_rules
      WHERE username = ?
      ORDER BY enabled DESC, priority ASC, id ASC`
    )
    .all(user.username);

  return { user, rules };
}

export function getUserByUsername(username: string) {
  const db = getDb();
  return db
    .prepare("SELECT * FROM user_catalog WHERE username = ? LIMIT 1")
    .get(username) as import("./types").User | undefined;
}

export function getUserSummary(username: string, origin?: string): UserSummary | undefined {
  const db = getDb();
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  const summary = db
    .prepare(
      `SELECT
        COUNT(*) AS total_sessions,
        SUM(user_message_count + assistant_message_count) AS total_messages,
        SUM(tool_call_count) AS total_tool_calls,
        SUM(total_input_tokens + total_output_tokens) AS total_tokens,
        COUNT(DISTINCT CASE
          WHEN project IS NOT NULL AND project != '' AND project != 'unknown'
          THEN project
        END) AS known_projects,
        COUNT(DISTINCT CASE
          WHEN repo_name IS NOT NULL AND repo_name != ''
          THEN repo_name
        END) AS known_repos,
        COALESCE(SUM(write_path_count), 0) AS write_paths,
        COALESCE(SUM(test_run_count), 0) AS test_runs,
        COALESCE(SUM(test_failure_count), 0) AS test_failures,
        COALESCE(SUM(build_run_count), 0) AS build_runs,
        COALESCE(SUM(build_failure_count), 0) AS build_failures,
        COALESCE(SUM(git_commit_count), 0) AS git_commits,
        COALESCE(SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'success' THEN 1 ELSE 0 END), 0) AS success_sessions,
        COALESCE(SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'partial' THEN 1 ELSE 0 END), 0) AS partial_sessions,
        COALESCE(SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'failed' THEN 1 ELSE 0 END), 0) AS failed_sessions,
        COALESCE(SUM(CASE WHEN COALESCE(session_status, 'exploration') = 'exploration' THEN 1 ELSE 0 END), 0) AS exploration_sessions,
        MIN(first_timestamp) AS first_seen,
        MAX(last_timestamp) AS last_seen
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}`
    )
    .get(...params) as UserSummary | undefined;
  if (!summary) return summary;
  // Event-dated active days: a session spanning N days counts N, not 1.
  const activeDays = db
    .prepare(
      `SELECT COUNT(DISTINCT d.day) AS active_days
      FROM session_daily_effective d
      JOIN session_catalog s ON s.session_id = d.session_id
      WHERE ${clauses.join(" AND ")}`
    )
    .get(...params) as { active_days: number } | undefined;
  return { ...summary, active_days: activeDays?.active_days ?? 0 };
}

/* ── User profile data ────────────────────────────────────────────── */

export function getUserActivity(username: string, days = 60, origin?: string) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  const clauses = [profileSessionClause("s"), "s.username = ?", "d.day >= ?"];
  const params: unknown[] = [username, cutoff];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        d.day AS day,
        COUNT(DISTINCT d.session_id) AS sessions,
        SUM(d.user_message_count + d.assistant_message_count) AS messages,
        SUM(d.tool_call_count) AS tool_calls
      FROM session_daily_effective d
      JOIN session_catalog s ON s.session_id = d.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY day
      ORDER BY day`
    )
    .all(...params) as {
    day: string;
    sessions: number;
    messages: number;
    tool_calls: number;
  }[];
}

/**
 * Daily GitHub contribution rollup for a user, aligned to a date range.
 * Returns an empty object if the user has no linked handle or no synced data.
 */
export function getUserGithubActivity(
  username: string,
  days = 60,
): Record<string, { contributions: number; prs_opened: number }> {
  const db = getDb();
  const cutoffDate = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  try {
    const rows = db
      .prepare(
        `SELECT day, contributions, prs_opened
         FROM user_github_daily
         WHERE username = ? AND day >= ?
         ORDER BY day`
      )
      .all(username, cutoffDate) as { day: string; contributions: number; prs_opened: number }[];
    const map: Record<string, { contributions: number; prs_opened: number }> = {};
    for (const r of rows) {
      map[r.day] = { contributions: r.contributions, prs_opened: r.prs_opened };
    }
    return map;
  } catch {
    // Table may not exist on un-migrated DBs
    return {};
  }
}

/** Totals for the hero meta line. Returns null if no data synced. */
export function getUserGithubTotals(
  username: string,
  days = 180,
): { contributions: number; prs_opened: number; since: string } | null {
  const db = getDb();
  const cutoffDate = new Date(Date.now() - days * 86400000).toISOString().slice(0, 10);
  try {
    const row = db
      .prepare(
        `SELECT SUM(contributions) AS contributions,
                SUM(prs_opened) AS prs_opened,
                MIN(day) AS since
         FROM user_github_daily
         WHERE username = ? AND day >= ?`
      )
      .get(username, cutoffDate) as
      | { contributions: number | null; prs_opened: number | null; since: string | null }
      | undefined;
    if (!row || !row.contributions) return null;
    return {
      contributions: row.contributions ?? 0,
      prs_opened: row.prs_opened ?? 0,
      since: row.since ?? cutoffDate,
    };
  } catch {
    return null;
  }
}

export function getUserSourceBreakdown(username: string, origin?: string) {
  const db = getDb();
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.source,
        COUNT(*) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      GROUP BY s.source
      ORDER BY sessions DESC`
    )
    .all(...params) as { source: string; sessions: number; messages: number }[];
}

export function getUserTopTools(username: string, limit = 12, origin?: string) {
  const db = getDb();
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT tc.tool_name, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY tc.tool_name
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(...params, limit) as { tool_name: string; cnt: number }[];
}

export function getUserModels(username: string, limit = 8, origin?: string) {
  const db = getDb();
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        CASE WHEN s.model IS NULL OR s.model = '' THEN 'unknown' ELSE s.model END AS model_name,
        COUNT(*) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      GROUP BY model_name
      ORDER BY sessions DESC
      LIMIT ?`
    )
    .all(...params, limit) as { model_name: string; sessions: number; messages: number }[];
}

export function getUserRecentSessions(username: string, limit = 12, origin?: string) {
  const db = getDb();
  const clauses = [profileSessionClause("s"), "s.username = ?"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT session_id, source, project, repo_name, model, session_goal,
              session_summary, session_outcome, session_status, first_timestamp,
              duration_seconds, user_message_count, assistant_message_count,
              tool_call_count
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, limit) as {
    session_id: string;
    source: string;
    project: string;
    repo_name: string | null;
    model: string | null;
    session_goal: string | null;
    session_summary: string | null;
    session_outcome: string | null;
    session_status: import("./types").SessionStatus | null;
    first_timestamp: string;
    duration_seconds: number | null;
    user_message_count: number;
    assistant_message_count: number;
    tool_call_count: number;
  }[];
}

/* ── Analysis ─────────────────────────────────────────────────────── */

export function getTopBashCommands(limit = 30, origin?: string) {
  const db = getDb();
  const clauses = [listedSessionClause("s", "u"), "tc.tool_name IN ('Bash', 'shell', 'bash')", "tc.command IS NOT NULL", "tc.command != ''"];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT tc.command, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY tc.command
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(...params, limit) as { command: string; cnt: number }[];
}

export function getSharedUtilities(limit = 20, origin?: string) {
  const db = getDb();
  const clauses = [listedSessionClause("s", "u"), "tc.command IS NOT NULL", "tc.command != ''"];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT tc.command, COUNT(DISTINCT s.username) AS users, COUNT(*) AS total
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${clauses.join(" AND ")}
      GROUP BY tc.command
      HAVING users >= 2
      ORDER BY users DESC, total DESC
      LIMIT ?`
    )
    .all(...params, limit) as { command: string; users: number; total: number }[];
}

export function getUserStats(origin?: string) {
  const db = getDb();
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.username AS username,
        s.user_display_name AS display_name,
        COUNT(*) AS sessions,
        SUM(s.user_message_count) AS user_msgs,
        SUM(s.tool_call_count) AS tool_calls,
        SUM(s.error_count) AS errors,
        SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
        MIN(s.first_timestamp) AS first_seen,
        MAX(s.last_timestamp) AS last_seen
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      GROUP BY username
      ORDER BY sessions DESC`
    )
    .all(...params) as {
    username: string;
    display_name: string;
    sessions: number;
    user_msgs: number;
    tool_calls: number;
    errors: number;
    tokens: number;
    first_seen: string;
    last_seen: string;
  }[];
}

function firstNonEmptyLine(text: string | null | undefined): string {
  if (!text) {
    return "";
  }
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (trimmed) {
      return trimmed;
    }
  }
  return "";
}

function objectiveSeedText(row: {
  session_goal?: string | null;
  first_user_message?: string | null;
  session_summary?: string | null;
}): string {
  return (
    firstNonEmptyLine(row.session_goal) ||
    firstNonEmptyLine(row.first_user_message) ||
    firstNonEmptyLine(row.session_summary)
  );
}

function normalizeObjectiveFamily(text: string): string | null {
  const firstLine = firstNonEmptyLine(text);
  if (!firstLine) {
    return null;
  }
  const normalized = firstLine
    .toLowerCase()
    .replace(/`[^`]+`/g, " <code> ")
    .replace(/https?:\/\/\S+/g, " <url> ")
    .replace(/\/[a-z0-9._~/-]+/gi, " <path> ")
    .replace(/\b[0-9a-f]{8,}\b/gi, " <id> ")
    .replace(/\b\d+\b/g, " <n> ")
    .replace(/[^a-z0-9<> ]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  return normalized || null;
}

function displayObjectiveLabel(text: string): string {
  return firstNonEmptyLine(text).replace(/\s+/g, " ").trim().slice(0, 110);
}

function normalizeObjectiveQuery(objective?: string): string | undefined {
  if (!objective) {
    return undefined;
  }
  const normalized = normalizeObjectiveFamily(objective);
  if (!normalized) {
    throw new RangeError(`Invalid objective filter: ${objective}`);
  }
  return normalized;
}

export function getRunawaySessions(limit = 8, origin?: string): RunawaySessionRow[] {
  const db = getDb();
  const clauses = [
    listedSessionClause("s", "u"),
    "(s.tool_call_count >= 200 OR s.error_count >= 25 OR (s.tool_call_count >= 100 AND s.error_count >= 10))",
  ];
  const params: unknown[] = [];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.session_id,
        s.source,
        s.username,
        s.username,
        s.user_display_name,
        s.project,
        s.repo_name,
        COALESCE(s.session_status, 'exploration') AS session_status,
        s.session_summary,
        s.first_timestamp,
        s.duration_seconds,
        s.tool_call_count,
        s.error_count
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY
        ((s.error_count * 5) + s.tool_call_count) DESC,
        COALESCE(s.duration_seconds, 0) DESC,
        s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, limit) as RunawaySessionRow[];
}

export function getRepeatedObjectiveRelaunches(limit = 8, origin?: string): ObjectiveRelaunchRow[] {
  const db = getDb();
  const cutoff = new Date(Date.now() - 30 * 86400000).toISOString();
  const clauses = [
    listedSessionClause("s", "u"),
    "s.first_timestamp >= ?",
    "(COALESCE(s.session_goal, '') != '' OR COALESCE(s.first_user_message, '') != '' OR COALESCE(s.session_summary, '') != '')",
  ];
  const params: unknown[] = [cutoff];
  appendOriginClause(clauses, params, origin);
  const rows = db
    .prepare(
      `SELECT
        s.session_id,
        s.username,
        s.username,
        s.user_display_name,
        s.project,
        s.repo_name,
        s.objective_family,
        s.objective_label,
        s.session_goal,
        s.first_user_message,
        s.session_summary,
        COALESCE(s.session_status, 'exploration') AS session_status,
        s.first_timestamp,
        s.tool_call_count,
        s.error_count
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      ORDER BY s.first_timestamp DESC`
    )
    .all(...params) as Array<{
    session_id: string;
    username: string;
    user_display_name: string;
    project: string | null;
    repo_name: string | null;
    objective_family: string | null;
    objective_label: string | null;
    session_goal: string | null;
    first_user_message: string | null;
    session_summary: string | null;
    session_status: Session["session_status"];
    first_timestamp: string | null;
    tool_call_count: number;
    error_count: number;
  }>;

  const grouped = new Map<string, ObjectiveRelaunchRow & { _operators: Set<string> }>();
  for (const row of rows) {
    const seed = objectiveSeedText(row);
    const objectiveKey = row.objective_family || normalizeObjectiveFamily(seed);
    if (!objectiveKey) {
      continue;
    }
    const operatorKey = row.username;
    const existing = grouped.get(objectiveKey);
    if (!existing) {
      grouped.set(objectiveKey, {
        objective_key: objectiveKey,
        display_label: row.objective_label || displayObjectiveLabel(seed),
        launches: 1,
        operator_count: 1,
        total_tool_calls: row.tool_call_count || 0,
        total_errors: row.error_count || 0,
        latest_timestamp: row.first_timestamp,
        latest_status: row.session_status,
        latest_session_id: row.session_id,
        latest_repo_name: row.repo_name || row.project,
        latest_summary: row.session_summary,
        _operators: new Set([operatorKey]),
      });
      continue;
    }
    existing.launches += 1;
    existing.total_tool_calls += row.tool_call_count || 0;
    existing.total_errors += row.error_count || 0;
    existing._operators.add(operatorKey);
    existing.operator_count = existing._operators.size;
  }

  return Array.from(grouped.values())
    .filter((row) => row.launches >= 2)
    .sort((a, b) => {
      if (b.launches !== a.launches) return b.launches - a.launches;
      if (b.total_tool_calls !== a.total_tool_calls) return b.total_tool_calls - a.total_tool_calls;
      if (b.total_errors !== a.total_errors) return b.total_errors - a.total_errors;
      return (b.latest_timestamp || "").localeCompare(a.latest_timestamp || "");
    })
    .slice(0, limit)
    .map((row) => {
      const { _operators: ignoredOperators, ...rest } = row;
      void ignoredOperators;
      return rest;
    });
}

type ContextExplosionRawRow = {
  session_id: string;
  root_session_id: string;
  parent_session_id: string | null;
  spawn_depth: number;
  username: string;
  user_display_name: string;
  project: string | null;
  repo_name: string | null;
  session_status: Session["session_status"];
  session_summary: string | null;
  first_timestamp: string | null;
  total_input_tokens: number;
  fresh_input_tokens: number;
  cached_input_tokens: number;
  total_output_tokens: number;
  tool_call_count: number;
  error_count: number;
  root_username: string;
  root_user_display_name: string;
  root_project: string | null;
  root_repo_name: string | null;
  root_session_goal: string | null;
  root_first_user_message: string | null;
  root_session_summary: string | null;
  root_first_timestamp: string | null;
  root_objective_label: string | null;
};

function contextExplosionWarnings(row: {
  child_session_count: number;
  cached_input_share: number;
  top_child_tokens: number;
  max_spawn_depth: number;
}): string[] {
  const warnings: string[] = [];
  if (row.cached_input_share >= 0.9) {
    warnings.push("mostly inherited context");
  }
  if (row.child_session_count >= 8) {
    warnings.push("fork swarm");
  }
  if (row.top_child_tokens >= 500_000_000) {
    warnings.push("giant child sessions");
  }
  if (row.max_spawn_depth >= 2) {
    warnings.push(`spawn depth ${row.max_spawn_depth}`);
  }
  return warnings;
}

export function getContextExplosionWorkstreams(limit = 6, origin?: string): ContextExplosionWorkstreamRow[] {
  const db = getDb();
  const cutoff = new Date(Date.now() - 7 * 86400000).toISOString();
  const clauses = [
    listedSessionClause("s", "u"),
    "s.source = 'codex'",
    "s.first_timestamp >= ?",
    "(s.parent_session_id IS NOT NULL OR EXISTS (SELECT 1 FROM sessions child WHERE child.parent_session_id = s.session_id))",
    "(COALESCE(s.total_input_tokens, 0) + COALESCE(s.total_output_tokens, 0)) > 0",
  ];
  const params: unknown[] = [cutoff];
  appendOriginClause(clauses, params, origin);

  const rows = db
    .prepare(
      `WITH RECURSIVE recent AS (
        SELECT s.session_id, s.parent_session_id
        FROM session_catalog s
        WHERE ${clauses.join(" AND ")}
      ),
      lineage AS (
        SELECT
          recent.session_id AS leaf_session_id,
          recent.session_id AS current_session_id,
          recent.parent_session_id AS parent_session_id
        FROM recent
        UNION ALL
        SELECT
          lineage.leaf_session_id,
          parent.session_id AS current_session_id,
          parent.parent_session_id AS parent_session_id
        FROM lineage
        JOIN sessions parent ON parent.session_id = lineage.parent_session_id
        WHERE lineage.parent_session_id IS NOT NULL
      ),
      roots AS (
        SELECT
          leaf_session_id,
          current_session_id AS root_session_id
        FROM lineage
        WHERE parent_session_id IS NULL
      )
      SELECT
        s.session_id,
        roots.root_session_id,
        s.parent_session_id,
        COALESCE(s.spawn_depth, 0) AS spawn_depth,
        s.username,
        s.user_display_name,
        s.project,
        s.repo_name,
        COALESCE(s.session_status, 'exploration') AS session_status,
        s.session_summary,
        s.first_timestamp,
        COALESCE(s.total_input_tokens, 0) AS total_input_tokens,
        COALESCE(s.fresh_input_tokens, 0) AS fresh_input_tokens,
        COALESCE(s.cached_input_tokens, 0) AS cached_input_tokens,
        COALESCE(s.total_output_tokens, 0) AS total_output_tokens,
        COALESCE(s.tool_call_count, 0) AS tool_call_count,
        COALESCE(s.error_count, 0) AS error_count,
        root.username AS root_username,
        root.user_display_name AS root_user_display_name,
        root.project AS root_project,
        root.repo_name AS root_repo_name,
        root.session_goal AS root_session_goal,
        root.first_user_message AS root_first_user_message,
        root.session_summary AS root_session_summary,
        root.first_timestamp AS root_first_timestamp,
        root.objective_label AS root_objective_label
      FROM recent
      JOIN roots ON roots.leaf_session_id = recent.session_id
      JOIN session_catalog s ON s.session_id = recent.session_id
      JOIN session_catalog root ON root.session_id = roots.root_session_id
      ORDER BY root.first_timestamp DESC, s.first_timestamp DESC`
    )
    .all(...params) as ContextExplosionRawRow[];

  const grouped = new Map<string, ContextExplosionWorkstreamRow>();
  for (const row of rows) {
    const totalTokens = (row.total_input_tokens || 0) + (row.total_output_tokens || 0);
    const existing = grouped.get(row.root_session_id);
    if (!existing) {
      const seed = objectiveSeedText({
        session_goal: row.root_session_goal,
        first_user_message: row.root_first_user_message,
        session_summary: row.root_session_summary,
      });
      const displayLabel =
        row.root_objective_label ||
        displayObjectiveLabel(seed || row.root_session_summary || row.session_summary || "Untitled workstream");
      grouped.set(row.root_session_id, {
        root_session_id: row.root_session_id,
        username: row.root_username,
        user_display_name: row.root_user_display_name,
        repo_name: row.root_repo_name || row.repo_name,
        project: row.root_project || row.project,
        display_label: displayLabel,
        root_summary: row.root_session_summary,
        root_first_timestamp: row.root_first_timestamp || row.first_timestamp,
        total_tokens: totalTokens,
        total_input_tokens: row.total_input_tokens || 0,
        fresh_input_tokens: row.fresh_input_tokens || 0,
        cached_input_tokens: row.cached_input_tokens || 0,
        total_output_tokens: row.total_output_tokens || 0,
        session_count: 1,
        child_session_count: row.session_id === row.root_session_id ? 0 : 1,
        max_spawn_depth: row.spawn_depth || 0,
        top_child_tokens: row.session_id === row.root_session_id ? 0 : totalTokens,
        child_token_share: row.session_id === row.root_session_id ? 0 : totalTokens,
        cached_input_share: 0,
        warnings: [],
        top_children: row.session_id === row.root_session_id
          ? []
          : [{
              session_id: row.session_id,
              agent_name: null,
              agent_role: null,
              total_tokens: totalTokens,
              total_input_tokens: row.total_input_tokens || 0,
              cached_input_tokens: row.cached_input_tokens || 0,
              total_output_tokens: row.total_output_tokens || 0,
              tool_call_count: row.tool_call_count || 0,
              error_count: row.error_count || 0,
              spawn_depth: row.spawn_depth || 0,
              first_timestamp: row.first_timestamp,
              is_root: false,
            }],
      });
      continue;
    }

    existing.total_tokens += totalTokens;
    existing.total_input_tokens += row.total_input_tokens || 0;
    existing.fresh_input_tokens += row.fresh_input_tokens || 0;
    existing.cached_input_tokens += row.cached_input_tokens || 0;
    existing.total_output_tokens += row.total_output_tokens || 0;
    existing.session_count += 1;
    existing.max_spawn_depth = Math.max(existing.max_spawn_depth, row.spawn_depth || 0);
    if (row.session_id !== row.root_session_id) {
      existing.child_session_count += 1;
      existing.child_token_share += totalTokens;
      existing.top_child_tokens = Math.max(existing.top_child_tokens, totalTokens);
      existing.top_children.push({
        session_id: row.session_id,
        agent_name: null,
        agent_role: null,
        total_tokens: totalTokens,
        total_input_tokens: row.total_input_tokens || 0,
        cached_input_tokens: row.cached_input_tokens || 0,
        total_output_tokens: row.total_output_tokens || 0,
        tool_call_count: row.tool_call_count || 0,
        error_count: row.error_count || 0,
        spawn_depth: row.spawn_depth || 0,
        first_timestamp: row.first_timestamp,
        is_root: false,
      });
    }
  }

  return Array.from(grouped.values())
    .map((row) => {
      row.child_token_share = row.total_tokens > 0 ? row.child_token_share / row.total_tokens : 0;
      row.cached_input_share = row.total_input_tokens > 0 ? row.cached_input_tokens / row.total_input_tokens : 0;
      row.top_children = row.top_children
        .sort((a, b) => b.total_tokens - a.total_tokens)
        .slice(0, 4);
      row.warnings = contextExplosionWarnings(row);
      return row;
    })
    .filter((row) => row.child_session_count >= 2 && row.total_tokens >= 250_000_000)
    .sort((a, b) => b.total_tokens - a.total_tokens)
    .slice(0, limit);
}

/* ── Repos ────────────────────────────────────────────────────────── */

export function getRepos(opts?: { user?: string; limit?: number }): RepoRow[] {
  const db = getDb();
  const { user, limit = 100 } = opts ?? {};
  const clauses = [listedSessionClause("s", "u"), "s.repo_name IS NOT NULL", "s.repo_name != ''"];
  const params: unknown[] = [];
  const repoGroupBy = config.publicMode ? "fs.repo_name" : "fs.repo_name, fs.repo_root";
  const repoRootColumn = config.publicMode ? "NULL AS repo_root" : "s.repo_root AS repo_root";
  const pathRootColumn = config.publicMode ? "NULL AS repo_root" : "fs.repo_root AS repo_root";
  const pathJoin = config.publicMode
    ? ""
    : "AND COALESCE(fs.repo_root, '') = COALESCE(pc.repo_root, '')";

  if (user) {
    clauses.push("(s.username = ? OR s.username = ?)");
    params.push(user, user);
  }

  const where = clauses.join(" AND ");
  return db
    .prepare(
      `WITH filtered_sessions AS (
        SELECT
          s.session_id,
          s.repo_name,
          ${repoRootColumn},
          s.worktree_root,
          s.git_branch,
          s.user_message_count,
          s.assistant_message_count,
          s.tool_call_count,
          s.last_timestamp
        FROM session_catalog s
        WHERE ${where}
      ),
      path_counts AS (
        SELECT
          fs.repo_name,
          ${pathRootColumn},
          COUNT(DISTINCT sp.normalized_path) AS unique_paths
        FROM filtered_sessions fs
        LEFT JOIN session_paths sp ON sp.session_id = fs.session_id
        GROUP BY ${repoGroupBy}
      )
      SELECT
        fs.repo_name,
        ${config.publicMode ? "NULL" : "fs.repo_root"} AS repo_root,
        COUNT(*) AS sessions,
        COUNT(DISTINCT fs.worktree_root) AS worktrees,
        COUNT(DISTINCT fs.git_branch) AS branches,
        SUM(fs.user_message_count + fs.assistant_message_count) AS messages,
        SUM(fs.tool_call_count) AS tool_calls,
        COALESCE(MAX(pc.unique_paths), 0) AS unique_paths,
        MAX(fs.last_timestamp) AS last_seen
      FROM filtered_sessions fs
      LEFT JOIN path_counts pc
        ON fs.repo_name = pc.repo_name
       ${pathJoin}
      GROUP BY ${repoGroupBy}
      ORDER BY sessions DESC
      LIMIT ?`
    )
    .all(...params, limit) as RepoRow[];
}

export function getReposForFilter() {
  const db = getDb();
  return db
    .prepare(
      `SELECT DISTINCT s.repo_name
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")}
        AND s.repo_name IS NOT NULL AND s.repo_name != ''
      ORDER BY s.repo_name`
    )
    .all() as { repo_name: string }[];
}

export function getUserTopRepos(username: string, limit = 8, origin?: string): RepoRow[] {
  const db = getDb();
  const repoRootSelect = config.publicMode ? "NULL" : "s.repo_root";
  const repoGroupBy = config.publicMode ? "s.repo_name" : "s.repo_name, s.repo_root";
  const clauses = [profileSessionClause("s"), "s.username = ?", "s.repo_name IS NOT NULL", "s.repo_name != ''"];
  const params: unknown[] = [username];
  appendOriginClause(clauses, params, origin);
  return db
    .prepare(
      `SELECT
        s.repo_name,
        ${repoRootSelect} AS repo_root,
        COUNT(*) AS sessions,
        COUNT(DISTINCT s.worktree_root) AS worktrees,
        COUNT(DISTINCT s.git_branch) AS branches,
        SUM(s.user_message_count + s.assistant_message_count) AS messages,
        SUM(s.tool_call_count) AS tool_calls,
        0 AS unique_paths,
        MAX(s.last_timestamp) AS last_seen
      FROM session_catalog s
      WHERE ${clauses.join(" AND ")}
      GROUP BY ${repoGroupBy}
      ORDER BY sessions DESC
      LIMIT ?`
    )
    .all(...params, limit) as RepoRow[];
}

/* ── Publish queue ────────────────────────────────────────────────── */

export function getPublishQueue(opts?: {
  visibility?: string;
  status?: string;
  user?: string;
  limit?: number;
}): import("./types").PublishCandidate[] {
  if (config.publicMode) return []; // publish queue is private-only

  const db = getDb();
  const { visibility = "pending", status, user, limit = 25 } = opts ?? {};
  const clauses: string[] = ["1 = 1"];
  const params: unknown[] = [];

  if (user) {
    clauses.push("(s.username = ? OR s.username = ?)");
    params.push(user, user);
  }

  const normVis = (visibility || "pending").toLowerCase();
  if (normVis === "pending") {
    clauses.push("s.visibility IN ('private', 'unlisted')");
  } else if (["private", "unlisted", "public"].includes(normVis)) {
    clauses.push("s.visibility = ?");
    params.push(normVis);
  }
  // "all" → no visibility filter

  if (status && ["exploration", "success", "partial", "failed"].includes(status)) {
    clauses.push("COALESCE(s.session_status, 'exploration') = ?");
    params.push(status);
  }

  const where = clauses.join(" AND ");
  return db
    .prepare(
      `SELECT
        s.session_id, s.source, s.username, s.username,
        COALESCE(u.display_name, s.username) AS display_name,
        s.project, s.repo_name, s.visibility,
        s.first_timestamp, s.last_timestamp,
        s.session_status, s.session_goal, s.session_summary, s.session_outcome
      FROM sessions s
      LEFT JOIN users u ON u.username = s.username
      WHERE ${where}
      ORDER BY
        CASE s.visibility WHEN 'unlisted' THEN 0 WHEN 'private' THEN 1 ELSE 2 END,
        s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, Math.min(Math.max(limit, 1), 200)) as import("./types").PublishCandidate[];
}
