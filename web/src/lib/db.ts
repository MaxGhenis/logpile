import Database from "better-sqlite3";
import { config } from "./config";
import type {
  ActivityFilter,
  DashboardStats,
  RepoRow,
  Session,
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

export function getDashboardStats(): DashboardStats {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        COUNT(*)                               AS total_sessions,
        SUM(user_message_count)                AS total_user_msgs,
        SUM(assistant_message_count)           AS total_assistant_msgs,
        SUM(tool_call_count)                   AS total_tool_calls,
        SUM(total_input_tokens)                AS total_input_tokens,
        SUM(total_output_tokens)               AS total_output_tokens,
        COUNT(DISTINCT user_slug)              AS active_users,
        COUNT(DISTINCT project)                AS total_projects
      FROM session_catalog s
      WHERE ${listedSessionClause("s")}`
    )
    .get() as DashboardStats;
}

export function getRecentSessions(limit = 10): SessionRow[] {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        s.session_id, s.source, s.username, s.user_slug,
        s.user_display_name,
        s.project, s.repo_name,
        s.session_goal, s.session_summary, s.session_outcome, s.session_status,
        s.first_timestamp,
        s.user_message_count, s.assistant_message_count,
        s.total_input_tokens + s.total_output_tokens AS tokens,
        s.first_user_message, s.duration_seconds, s.model
      FROM session_catalog s
      WHERE ${listedSessionClause("s")}
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(limit) as SessionRow[];
}

/* ── Chart data ───────────────────────────────────────────────────── */

export function getMessagesPerDay(days = 30) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString();
  const rows = db
    .prepare(
      `SELECT
        substr(s.first_timestamp, 1, 10) AS day,
        COALESCE(s.user_slug, s.username) AS user_key,
        s.username,
        COALESCE(u.display_name, s.username) AS user_display_name,
        SUM(s.user_message_count + s.assistant_message_count) AS msgs
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")} AND s.first_timestamp >= ?
      GROUP BY day, user_key, s.username, user_display_name
      ORDER BY day`
    )
    .all(cutoff) as {
      day: string;
      user_key: string;
      username: string;
      user_display_name: string;
      msgs: number;
    }[];
  return rows;
}

export function getMessagesByTool(days = 30) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString();
  return db
    .prepare(
      `SELECT
        substr(first_timestamp, 1, 10) AS day,
        source,
        SUM(user_message_count + assistant_message_count) AS msgs
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")} AND first_timestamp >= ?
      GROUP BY day, source
      ORDER BY day`
    )
    .all(cutoff) as { day: string; source: string; msgs: number }[];
}

export function getTopTools(limit = 20) {
  const db = getDb();
  return db
    .prepare(
      `SELECT tc.tool_name, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${listedSessionClause("s", "u")}
      GROUP BY tc.tool_name
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(limit) as { tool_name: string; cnt: number }[];
}

export function getErrorRate(limit = 15) {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        COALESCE(s.user_slug, s.username) AS user_key,
        s.username,
        COALESCE(u.display_name, s.username) AS user_display_name,
        SUM(s.error_count) AS errors,
        SUM(s.tool_call_count) AS tools
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")}
      GROUP BY user_key, s.username, user_display_name
      ORDER BY errors DESC
      LIMIT ?`
    )
    .all(limit) as {
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

export function getSessions(opts: {
  q?: string;
  source?: string;
  user?: string;
  repo?: string;
  repoRoot?: string;
  branch?: string;
  activity?: string;
  status?: string;
  page?: number;
  perPage?: number;
}) {
  const db = getDb();
  const {
    q,
    source,
    user,
    repo,
    repoRoot,
    branch,
    activity,
    status,
    page = 1,
    perPage = 50,
  } = opts;
  const normalizedActivity = normalizeActivityFilter(activity);
  const normalizedStatus = normalizeSessionStatus(status);
  const clauses = [listedSessionClause("s", "u")];
  const params: unknown[] = [];

  if (q) {
    clauses.push("s.first_user_message LIKE ?");
    params.push(`%${q}%`);
  }
  if (source) {
    clauses.push("s.source = ?");
    params.push(source);
  }
  if (user) {
    clauses.push("(s.user_slug = ? OR s.username = ?)");
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
        s.session_id, s.source, s.username, s.user_slug, s.project,
        s.repo_name, ${config.publicMode ? "NULL" : "s.repo_root"} AS repo_root, s.git_branch,
        s.user_display_name AS user_display_name,
        s.session_goal, s.session_summary, s.session_outcome, s.session_status,
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
      LEFT JOIN user_catalog u ON u.slug = s.user_slug
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
        u.slug,
        COALESCE(u.display_name, u.username) AS display_name
      FROM user_catalog u
      WHERE ${listedProfileClause("u")}
        AND EXISTS (
          SELECT 1
          FROM session_catalog s
          WHERE s.user_slug = u.slug AND ${listedSessionClause("s")}
        )
      ORDER BY display_name`
    )
    .all() as { slug: string; display_name: string }[];
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
      LEFT JOIN user_catalog u ON u.slug = s.user_slug
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
        u.slug,
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
      LEFT JOIN session_catalog s ON s.user_slug = u.slug AND ${listedSessionClause("s", "u")}
      WHERE ${listedProfileClause("u")}
      GROUP BY u.slug, u.username, u.display_name, u.bio
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
        s.user_slug,
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
  branch?: string;
  activity?: string;
  status?: string;
  user?: string;
  path?: string;
  limit?: number;
}) {
  const db = getDb();
  const normalizedActivity = normalizeActivityFilter(opts.activity);
  const normalizedStatus = normalizeSessionStatus(opts.status);
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
  if (opts.user) {
    clauses.push("(s.user_slug = ? OR s.username = ?)");
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
        s.user_slug,
        s.user_display_name,
        s.project,
        s.repo_name,
        s.session_goal,
        s.session_summary,
        s.session_outcome,
        s.session_status,
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
        u.slug,
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
      LEFT JOIN session_catalog s ON s.user_slug = u.slug AND ${listedSessionClause("s", "u")}
      WHERE ${listedProfileClause("u")}
      GROUP BY u.slug, u.username, u.display_name, u.bio, u.avatar_url, u.profile_visibility
      ORDER BY last_seen DESC, sessions DESC, u.slug`
    )
    .all() as Array<{
      slug: string;
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

export function getApiUserProfile(slug: string) {
  const user = getUserBySlug(slug);
  if (!user || !isProfileDirectlyVisible(user)) {
    return null;
  }

  const summary = getUserSummary(user.slug);
  return { user, summary };
}

export function getApiUserSessions(
  slug: string,
  {
    limit = 50,
    offset = 0,
    project,
    repo,
    branch,
    activity,
    status,
    path,
  }: {
    limit?: number;
    offset?: number;
    project?: string;
    repo?: string;
    branch?: string;
    activity?: string;
    status?: string;
    path?: string;
  } = {}
) {
  const user = getUserBySlug(slug);
  if (!user || !isProfileDirectlyVisible(user)) {
    return null;
  }

  const db = getDb();
  const normalizedActivity = normalizeActivityFilter(activity);
  const normalizedStatus = normalizeSessionStatus(status);
  const clampedLimit = Math.min(Math.max(limit, 1), 200);
  const clampedOffset = Math.max(offset, 0);
  const clauses = [profileSessionClause("s"), "s.user_slug = ?"];
  const params: unknown[] = [user.slug];

  if (project) {
    clauses.push("s.project = ?");
    params.push(project);
  }
  if (repo) {
    clauses.push("COALESCE(s.repo_name, '') = ?");
    params.push(repo);
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

export function getApiUserRules(slug: string) {
  if (config.publicMode) {
    return null;
  }

  const user = getUserBySlug(slug);
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
      WHERE user_slug = ?
      ORDER BY enabled DESC, priority ASC, id ASC`
    )
    .all(user.slug);

  return { user, rules };
}

export function getUserBySlug(slug: string) {
  const db = getDb();
  return db
    .prepare("SELECT * FROM user_catalog WHERE slug = ? OR username = ? LIMIT 1")
    .get(slug, slug) as import("./types").User | undefined;
}

export function getUserSummary(userSlug: string): UserSummary | undefined {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        COUNT(*) AS total_sessions,
        SUM(user_message_count + assistant_message_count) AS total_messages,
        SUM(tool_call_count) AS total_tool_calls,
        SUM(total_input_tokens + total_output_tokens) AS total_tokens,
        COUNT(DISTINCT substr(first_timestamp, 1, 10)) AS active_days,
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
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?`
    )
    .get(userSlug) as UserSummary | undefined;
}

/* ── User profile data ────────────────────────────────────────────── */

export function getUserActivity(userSlug: string, days = 60) {
  const db = getDb();
  const cutoff = new Date(Date.now() - days * 86400000).toISOString();
  return db
    .prepare(
      `SELECT
        substr(s.first_timestamp, 1, 10) AS day,
        COUNT(*) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages,
        SUM(s.tool_call_count) AS tool_calls
      FROM session_catalog s
      WHERE ${profileSessionClause("s")} AND s.user_slug = ? AND s.first_timestamp >= ?
      GROUP BY day
      ORDER BY day`
    )
    .all(userSlug, cutoff) as {
    day: string;
    sessions: number;
    messages: number;
    tool_calls: number;
  }[];
}

export function getUserSourceBreakdown(userSlug: string) {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        s.source,
        COUNT(*) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages
      FROM session_catalog s
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?
      GROUP BY s.source
      ORDER BY sessions DESC`
    )
    .all(userSlug) as { source: string; sessions: number; messages: number }[];
}

export function getUserTopTools(userSlug: string, limit = 12) {
  const db = getDb();
  return db
    .prepare(
      `SELECT tc.tool_name, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?
      GROUP BY tc.tool_name
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(userSlug, limit) as { tool_name: string; cnt: number }[];
}

export function getUserModels(userSlug: string, limit = 8) {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        CASE WHEN s.model IS NULL OR s.model = '' THEN 'unknown' ELSE s.model END AS model_name,
        COUNT(*) AS sessions,
        SUM(s.user_message_count + s.assistant_message_count) AS messages
      FROM session_catalog s
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?
      GROUP BY model_name
      ORDER BY sessions DESC
      LIMIT ?`
    )
    .all(userSlug, limit) as { model_name: string; sessions: number; messages: number }[];
}

export function getUserRecentSessions(userSlug: string, limit = 12) {
  const db = getDb();
  return db
    .prepare(
      `SELECT session_id, source, project, repo_name, model, session_goal,
              session_summary, session_outcome, session_status, first_timestamp,
              duration_seconds, user_message_count, assistant_message_count,
              tool_call_count
      FROM session_catalog s
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?
      ORDER BY s.first_timestamp DESC
      LIMIT ?`
    )
    .all(userSlug, limit) as {
    session_id: string;
    source: string;
    project: string;
    repo_name: string | null;
    model: string | null;
    first_timestamp: string;
    duration_seconds: number | null;
    user_message_count: number;
    assistant_message_count: number;
    tool_call_count: number;
  }[];
}

/* ── Analysis ─────────────────────────────────────────────────────── */

export function getTopBashCommands(limit = 30) {
  const db = getDb();
  return db
    .prepare(
      `SELECT tc.command, COUNT(*) AS cnt
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${listedSessionClause("s", "u")}
        AND tc.tool_name IN ('Bash', 'shell', 'bash')
        AND tc.command IS NOT NULL AND tc.command != ''
      GROUP BY tc.command
      ORDER BY cnt DESC
      LIMIT ?`
    )
    .all(limit) as { command: string; cnt: number }[];
}

export function getSharedUtilities(limit = 20) {
  const db = getDb();
  return db
    .prepare(
      `SELECT tc.command, COUNT(DISTINCT s.username) AS users, COUNT(*) AS total
      FROM tool_calls tc
      JOIN session_catalog s ON s.session_id = tc.session_id
      WHERE ${listedSessionClause("s", "u")}
        AND tc.command IS NOT NULL AND tc.command != ''
      GROUP BY tc.command
      HAVING users >= 2
      ORDER BY users DESC, total DESC
      LIMIT ?`
    )
    .all(limit) as { command: string; users: number; total: number }[];
}

export function getUserStats() {
  const db = getDb();
  return db
    .prepare(
      `SELECT
        COALESCE(s.user_slug, s.username) AS slug,
        s.user_display_name AS display_name,
        COUNT(*) AS sessions,
        SUM(s.user_message_count) AS user_msgs,
        SUM(s.tool_call_count) AS tool_calls,
        SUM(s.error_count) AS errors,
        SUM(s.total_input_tokens + s.total_output_tokens) AS tokens,
        MIN(s.first_timestamp) AS first_seen,
        MAX(s.last_timestamp) AS last_seen
      FROM session_catalog s
      WHERE ${listedSessionClause("s", "u")}
      GROUP BY slug
      ORDER BY sessions DESC`
    )
    .all() as {
    slug: string;
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
    clauses.push("(s.user_slug = ? OR s.username = ?)");
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

export function getUserTopRepos(userSlug: string, limit = 8): RepoRow[] {
  const db = getDb();
  const repoRootSelect = config.publicMode ? "NULL" : "s.repo_root";
  const repoGroupBy = config.publicMode ? "s.repo_name" : "s.repo_name, s.repo_root";
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
      WHERE ${profileSessionClause("s")} AND s.user_slug = ?
        AND s.repo_name IS NOT NULL AND s.repo_name != ''
      GROUP BY ${repoGroupBy}
      ORDER BY sessions DESC
      LIMIT ?`
    )
    .all(userSlug, limit) as RepoRow[];
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
    clauses.push("(s.user_slug = ? OR s.username = ?)");
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
        s.session_id, s.source, s.username, s.user_slug,
        COALESCE(u.display_name, s.username) AS display_name,
        s.project, s.repo_name, s.visibility,
        s.first_timestamp, s.last_timestamp,
        s.session_status, s.session_goal, s.session_summary, s.session_outcome
      FROM sessions s
      LEFT JOIN users u ON u.slug = s.user_slug
      WHERE ${where}
      ORDER BY
        CASE s.visibility WHEN 'unlisted' THEN 0 WHEN 'private' THEN 1 ELSE 2 END,
        s.first_timestamp DESC
      LIMIT ?`
    )
    .all(...params, Math.min(Math.max(limit, 1), 200)) as import("./types").PublishCandidate[];
}
