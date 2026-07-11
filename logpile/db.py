"""SQLite database for the Logpile session index."""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .origins import SESSION_ORIGINS


SESSION_VISIBILITIES = ("private", "unlisted", "public")
PROFILE_VISIBILITIES = ("private", "unlisted", "public")

# Parses at or above this token_version emit message_claims rows, so their
# native_* columns are claims-derived. Older rows (whose bytes may be gone)
# keep native_* mirroring transcript totals — the pre-dedup approximation.
CLAIMS_TOKEN_VERSION = 5

# (native column, transcript column) pairs shared by `sessions` and
# `session_daily_usage`. native_* = usage first attributed to this session:
# for claudecode, inherited resume history is excluded via message_claims;
# for codex, parse-time replay-burst handling already makes transcript
# totals live-only, so native mirrors them.
NATIVE_TOKEN_COLUMNS = (
    ("native_total_input_tokens", "total_input_tokens"),
    ("native_total_output_tokens", "total_output_tokens"),
    ("native_fresh_input_tokens", "fresh_input_tokens"),
    ("native_cached_input_tokens", "cached_input_tokens"),
    ("native_cache_creation_input_tokens", "cache_creation_input_tokens"),
    ("native_cache_creation_5m_input_tokens", "cache_creation_5m_input_tokens"),
    ("native_cache_creation_1h_input_tokens", "cache_creation_1h_input_tokens"),
    ("native_reasoning_output_tokens", "reasoning_output_tokens"),
    ("native_assistant_message_count", "assistant_message_count"),
)
RULE_MATCH_FIELDS = (
    "project",
    "source_path",
    "first_user_message",
    "model",
    "machine",
    "username",
)
RULE_MATCH_MODES = ("equals", "contains", "prefix", "suffix", "regex", "fuzzy")
RULE_SOURCE_SCOPES = ("claudecode", "codex")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username TEXT PRIMARY KEY,
    display_name TEXT,
    bio TEXT,
    avatar_url TEXT,
    profile_visibility TEXT NOT NULL DEFAULT 'public',
    default_session_visibility TEXT NOT NULL DEFAULT 'unlisted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_visibility_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL,
    source_scope TEXT,
    field TEXT NOT NULL,
    match_mode TEXT NOT NULL,
    pattern TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'public',
    priority INTEGER NOT NULL DEFAULT 100,
    threshold REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    username       TEXT NOT NULL,
    machine        TEXT,
    project        TEXT,
    workspace_root TEXT,
    worktree_root  TEXT,
    repo_root      TEXT,
    repo_name      TEXT,
    git_branch     TEXT,
    git_commit     TEXT,
    git_dirty      INTEGER DEFAULT 0,
    source_path    TEXT NOT NULL,
    shared_path    TEXT NOT NULL,
    first_timestamp TEXT,
    last_timestamp  TEXT,
    duration_seconds REAL,
    user_message_count    INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count       INTEGER DEFAULT 0,
    error_count           INTEGER DEFAULT 0,
    write_path_count      INTEGER,
    read_path_count       INTEGER,
    search_path_count     INTEGER,
    test_run_count        INTEGER,
    test_failure_count    INTEGER,
    lint_run_count        INTEGER,
    lint_failure_count    INTEGER,
    build_run_count       INTEGER,
    build_failure_count   INTEGER,
    format_run_count      INTEGER,
    format_failure_count  INTEGER,
    git_status_count      INTEGER,
    git_diff_count        INTEGER,
    git_commit_count      INTEGER,
    activity_version      INTEGER DEFAULT 0,
    session_goal          TEXT,
    session_summary       TEXT,
    session_outcome       TEXT,
    session_status        TEXT,
    narrative_version     INTEGER DEFAULT 0,
    objective_family      TEXT,
    objective_label       TEXT,
    objective_version     INTEGER DEFAULT 0,
    session_origin        TEXT,
    origin_version        INTEGER DEFAULT 0,
    total_input_tokens    INTEGER DEFAULT 0,
    total_output_tokens   INTEGER DEFAULT 0,
    fresh_input_tokens    INTEGER DEFAULT 0,
    cached_input_tokens   INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    cache_creation_5m_input_tokens INTEGER DEFAULT 0,
    cache_creation_1h_input_tokens INTEGER DEFAULT 0,
    reasoning_output_tokens INTEGER DEFAULT 0,
    native_total_input_tokens    INTEGER DEFAULT 0,
    native_total_output_tokens   INTEGER DEFAULT 0,
    native_fresh_input_tokens    INTEGER DEFAULT 0,
    native_cached_input_tokens   INTEGER DEFAULT 0,
    native_cache_creation_input_tokens INTEGER DEFAULT 0,
    native_cache_creation_5m_input_tokens INTEGER DEFAULT 0,
    native_cache_creation_1h_input_tokens INTEGER DEFAULT 0,
    native_reasoning_output_tokens INTEGER DEFAULT 0,
    native_assistant_message_count INTEGER DEFAULT 0,
    token_version        INTEGER DEFAULT 0,
    first_user_message    TEXT,
    parent_session_id    TEXT,
    spawn_depth          INTEGER DEFAULT 0,
    visibility     TEXT NOT NULL DEFAULT 'public',
    visibility_source TEXT NOT NULL DEFAULT 'default',
    visibility_rule_id INTEGER,
    visibility_reason TEXT,
    is_private     INTEGER DEFAULT 0,
    file_hash      TEXT,
    file_size      INTEGER,
    file_mtime     REAL,
    synced_at      TEXT,
    model          TEXT
);

-- Usage bucketed by UTC day of the underlying events. Sessions can span
-- weeks, so date-bucketed rollups must come from here, not from attributing
-- whole-session totals to first_timestamp.
CREATE TABLE IF NOT EXISTS session_daily_usage (
    session_id     TEXT NOT NULL,
    day            TEXT NOT NULL,
    total_input_tokens  INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    fresh_input_tokens  INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    native_total_input_tokens    INTEGER NOT NULL DEFAULT 0,
    native_total_output_tokens   INTEGER NOT NULL DEFAULT 0,
    native_fresh_input_tokens    INTEGER NOT NULL DEFAULT 0,
    native_cached_input_tokens   INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    native_assistant_message_count INTEGER NOT NULL DEFAULT 0,
    user_message_count      INTEGER NOT NULL DEFAULT 0,
    assistant_message_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count         INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, day)
);

-- Cross-session dedup ledger for Claude Code assistant messages. Resuming a
-- CC session copies prior history into a new transcript re-stamped with the
-- new sessionId, so the same (message.id, requestId) appears in every file
-- of a resume chain. Each parsed message claims its key here; the owner is
-- the session with the smallest (last_timestamp, first_timestamp,
-- session_id) among claimants — the earliest-ending transcript containing
-- the message, i.e. the session where it ran live (a resume copy always
-- ends at or after its source). native_* columns aggregate from here.
CREATE TABLE IF NOT EXISTS message_claims (
    claim_key TEXT PRIMARY KEY,
    owner_session_id TEXT NOT NULL,
    day TEXT,
    model TEXT,
    fresh_input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
) WITHOUT ROWID;

-- Small key/value state for the sync pipeline itself (e.g. whether a
-- native_* refresh is owed after an interrupted sync).
CREATE TABLE IF NOT EXISTS logpile_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL,
    tool_name   TEXT NOT NULL,
    command     TEXT,
    timestamp   TEXT,
    is_error    INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_paths (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    raw_path         TEXT NOT NULL,
    normalized_path  TEXT NOT NULL,
    relative_path    TEXT,
    repo_relative_path TEXT,
    display_path     TEXT NOT NULL,
    operation        TEXT NOT NULL,
    source           TEXT NOT NULL,
    tool_name        TEXT,
    first_timestamp  TEXT,
    last_timestamp   TEXT,
    occurrence_count INTEGER NOT NULL DEFAULT 1
);

"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_users_profile_visibility     ON users(profile_visibility);
CREATE INDEX IF NOT EXISTS idx_sessions_source              ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_username            ON sessions(username);
CREATE INDEX IF NOT EXISTS idx_sessions_ts                  ON sessions(first_timestamp);
CREATE INDEX IF NOT EXISTS idx_sessions_project             ON sessions(project);
CREATE INDEX IF NOT EXISTS idx_sessions_workspace_root      ON sessions(workspace_root);
CREATE INDEX IF NOT EXISTS idx_sessions_worktree_root       ON sessions(worktree_root);
CREATE INDEX IF NOT EXISTS idx_sessions_repo_root           ON sessions(repo_root);
CREATE INDEX IF NOT EXISTS idx_sessions_repo_name           ON sessions(repo_name);
CREATE INDEX IF NOT EXISTS idx_sessions_git_branch          ON sessions(git_branch);
CREATE INDEX IF NOT EXISTS idx_sessions_visibility          ON sessions(visibility);
CREATE INDEX IF NOT EXISTS idx_sessions_visibility_source   ON sessions(visibility_source);
CREATE INDEX IF NOT EXISTS idx_sessions_status              ON sessions(session_status);
CREATE INDEX IF NOT EXISTS idx_sessions_objective_family    ON sessions(objective_family);
CREATE INDEX IF NOT EXISTS idx_sessions_origin              ON sessions(session_origin);
CREATE INDEX IF NOT EXISTS idx_sessions_parent_session      ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_session_daily_day            ON session_daily_usage(day);
CREATE INDEX IF NOT EXISTS idx_message_claims_owner_day     ON message_claims(owner_session_id, day);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session           ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name              ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_session_paths_session        ON session_paths(session_id);
CREATE INDEX IF NOT EXISTS idx_session_paths_display        ON session_paths(display_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_relative       ON session_paths(relative_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_repo_relative  ON session_paths(repo_relative_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_normalized     ON session_paths(normalized_path);
CREATE INDEX IF NOT EXISTS idx_rules_username               ON session_visibility_rules(username);
CREATE INDEX IF NOT EXISTS idx_rules_priority               ON session_visibility_rules(username, enabled, priority, id);
"""

VIEWS = """
DROP VIEW IF EXISTS session_catalog;
CREATE VIEW session_catalog AS
SELECT
    s.*,
    COALESCE(u.display_name, s.username) AS user_display_name,
    COALESCE(u.profile_visibility, 'public') AS user_profile_visibility,
    COALESCE(u.default_session_visibility, 'unlisted') AS user_default_session_visibility,
    CASE
        WHEN s.visibility = 'public' AND COALESCE(u.profile_visibility, 'public') = 'public'
        THEN 1 ELSE 0
    END AS listed_public,
    CASE
        WHEN s.visibility IN ('public', 'unlisted')
        THEN 1 ELSE 0
    END AS listed_private,
    CASE
        WHEN s.visibility = 'public'
         AND COALESCE(u.profile_visibility, 'public') IN ('public', 'unlisted')
        THEN 1 ELSE 0
    END AS direct_public,
    CASE
        WHEN s.visibility IN ('public', 'unlisted')
        THEN 1 ELSE 0
    END AS direct_private
FROM sessions s
LEFT JOIN users u ON u.username = s.username;

-- Per-day usage with graceful degradation: sessions that have not been
-- re-synced since session_daily_usage was introduced (or whose records carry
-- no timestamps) fall back to whole-session totals attributed to the start
-- date, flagged approximated = 1.
DROP VIEW IF EXISTS session_daily_effective;
CREATE VIEW session_daily_effective AS
SELECT
    d.session_id,
    d.day,
    d.total_input_tokens,
    d.total_output_tokens,
    d.fresh_input_tokens,
    d.cached_input_tokens,
    d.cache_creation_input_tokens,
    d.cache_creation_5m_input_tokens,
    d.cache_creation_1h_input_tokens,
    d.reasoning_output_tokens,
    d.native_total_input_tokens,
    d.native_total_output_tokens,
    d.native_fresh_input_tokens,
    d.native_cached_input_tokens,
    d.native_cache_creation_input_tokens,
    d.native_cache_creation_5m_input_tokens,
    d.native_cache_creation_1h_input_tokens,
    d.native_reasoning_output_tokens,
    d.native_assistant_message_count,
    d.user_message_count,
    d.assistant_message_count,
    d.tool_call_count,
    0 AS approximated
FROM session_daily_usage d
UNION ALL
SELECT
    s.session_id,
    substr(s.first_timestamp, 1, 10) AS day,
    COALESCE(s.total_input_tokens, 0),
    COALESCE(s.total_output_tokens, 0),
    COALESCE(s.fresh_input_tokens, 0),
    COALESCE(s.cached_input_tokens, 0),
    COALESCE(s.cache_creation_input_tokens, 0),
    COALESCE(s.cache_creation_5m_input_tokens, 0),
    COALESCE(s.cache_creation_1h_input_tokens, 0),
    COALESCE(s.reasoning_output_tokens, 0),
    COALESCE(s.native_total_input_tokens, 0),
    COALESCE(s.native_total_output_tokens, 0),
    COALESCE(s.native_fresh_input_tokens, 0),
    COALESCE(s.native_cached_input_tokens, 0),
    COALESCE(s.native_cache_creation_input_tokens, 0),
    COALESCE(s.native_cache_creation_5m_input_tokens, 0),
    COALESCE(s.native_cache_creation_1h_input_tokens, 0),
    COALESCE(s.native_reasoning_output_tokens, 0),
    COALESCE(s.native_assistant_message_count, 0),
    COALESCE(s.user_message_count, 0),
    COALESCE(s.assistant_message_count, 0),
    COALESCE(s.tool_call_count, 0),
    1 AS approximated
FROM sessions s
WHERE s.first_timestamp IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM session_daily_usage d2 WHERE d2.session_id = s.session_id
  );

DROP VIEW IF EXISTS user_catalog;
CREATE VIEW user_catalog AS
SELECT
    u.username,
    COALESCE(u.display_name, u.username) AS display_name,
    u.bio,
    u.avatar_url,
    COALESCE(u.profile_visibility, 'public') AS profile_visibility,
    COALESCE(u.default_session_visibility, 'unlisted') AS default_session_visibility,
    u.created_at,
    u.updated_at,
    CASE
        WHEN COALESCE(u.profile_visibility, 'public') = 'public'
        THEN 1 ELSE 0
    END AS listed_public,
    1 AS listed_private,
    CASE
        WHEN COALESCE(u.profile_visibility, 'public') IN ('public', 'unlisted')
        THEN 1 ELSE 0
    END AS direct_public,
    1 AS direct_private
FROM users u;
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return normalized or "user"


def _normalize_visibility(value: str | None, allowed: tuple[str, ...], default: str) -> str:
    if not value:
        return default
    normalized = value.strip().lower()
    return normalized if normalized in allowed else default


def _normalize_rule_field(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in RULE_MATCH_FIELDS:
        raise ValueError(f"Unsupported rule field: {value}")
    return normalized


def _normalize_rule_mode(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    if normalized not in RULE_MATCH_MODES:
        raise ValueError(f"Unsupported rule match mode: {value}")
    return normalized


def _normalize_source_scope(value: str | None) -> str | None:
    if value in (None, "", "*"):
        return None
    normalized = value.strip().lower()
    if normalized not in RULE_SOURCE_SCOPES:
        raise ValueError(f"Unsupported rule source scope: {value}")
    return normalized


def _normalize_threshold(match_mode: str, threshold: float | None) -> float | None:
    if match_mode != "fuzzy":
        return None
    if threshold is None:
        return 0.7
    try:
        numeric = float(threshold)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, numeric))


def _normalize_text(value: str | None) -> str:
    lowered = (value or "").strip().lower()
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def _fuzzy_score(value: str | None, pattern: str | None) -> float:
    normalized_value = _normalize_text(value)
    normalized_pattern = _normalize_text(pattern)
    if not normalized_value or not normalized_pattern:
        return 0.0
    if normalized_value == normalized_pattern:
        return 1.0
    if normalized_pattern in normalized_value:
        return 0.98

    compact_value = normalized_value.replace(" ", "")
    compact_pattern = normalized_pattern.replace(" ", "")
    sequence_score = SequenceMatcher(None, normalized_pattern, normalized_value).ratio()
    compact_score = SequenceMatcher(None, compact_pattern, compact_value).ratio()

    pattern_tokens = set(normalized_pattern.split())
    value_tokens = set(normalized_value.split())
    token_score = (
        len(pattern_tokens & value_tokens) / len(pattern_tokens)
        if pattern_tokens
        else 0.0
    )

    return max(sequence_score, compact_score, token_score)


def _rule_match_score(
    value: str | None,
    *,
    pattern: str,
    match_mode: str,
    threshold: float | None,
) -> float | None:
    haystack = (value or "").strip()
    needle = (pattern or "").strip()
    if not haystack or not needle:
        return None

    left = haystack.lower()
    right = needle.lower()

    if match_mode == "equals":
        return 1.0 if left == right else None
    if match_mode == "contains":
        return 1.0 if right in left else None
    if match_mode == "prefix":
        return 1.0 if left.startswith(right) else None
    if match_mode == "suffix":
        return 1.0 if left.endswith(right) else None
    if match_mode == "regex":
        try:
            return 1.0 if re.search(pattern, haystack, flags=re.IGNORECASE) else None
        except re.error:
            return None
    if match_mode == "fuzzy":
        score = _fuzzy_score(haystack, needle)
        return score if score >= (threshold or 0.7) else None
    return None


def _table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {row[1] for row in rows}


def _ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, spec: str) -> None:
    if column_name in _table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {spec}")


def _next_available_username(conn: sqlite3.Connection, base_username: str) -> str:
    username = base_username
    suffix = 2
    while conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
        username = f"{base_username}-{suffix}"
        suffix += 1
    return username


def ensure_user(conn: sqlite3.Connection, username: str, display_name: str | None = None) -> str:
    canonical_username = normalize_username(username)
    row = conn.execute(
        "SELECT username, display_name FROM users WHERE username = ? LIMIT 1",
        (canonical_username,),
    ).fetchone()
    now = _now_iso()
    if row:
        if display_name and not row["display_name"]:
            conn.execute(
                "UPDATE users SET display_name = ?, updated_at = ? WHERE username = ?",
                (display_name, now, row["username"]),
            )
        return row["username"]

    base_username = normalize_username(username)
    canonical_username = _next_available_username(conn, base_username)
    conn.execute(
        """
        INSERT INTO users (
            username, display_name, bio, avatar_url,
            profile_visibility, default_session_visibility,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            canonical_username,
            display_name or username,
            None,
            None,
            "public",
            "unlisted",
            now,
            now,
        ),
    )
    return canonical_username


def get_user_by_identifier(conn: sqlite3.Connection, identifier: str):
    normalized = normalize_username(identifier)
    return conn.execute(
        """
        SELECT *
        FROM users
        WHERE username IN (?, ?)
        ORDER BY CASE WHEN username = ? THEN 0 ELSE 1 END
        LIMIT 1
        """,
        (identifier, normalized, identifier),
    ).fetchone()


def list_users(conn: sqlite3.Connection):
    return conn.execute(
        """
        SELECT
            username,
            COALESCE(display_name, username) AS display_name,
            profile_visibility,
            default_session_visibility,
            created_at,
            updated_at
        FROM users
        ORDER BY updated_at DESC, username
        """
    ).fetchall()


def update_user(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    display_name: str | None = None,
    bio: str | None = None,
    avatar_url: str | None = None,
    profile_visibility: str | None = None,
    default_session_visibility: str | None = None,
    github_username: str | None = None,
):
    user = get_user_by_identifier(conn, identifier)
    if not user:
        return None

    updates: dict[str, str] = {"updated_at": _now_iso()}
    if display_name is not None:
        updates["display_name"] = display_name
    if bio is not None:
        updates["bio"] = bio
    if avatar_url is not None:
        updates["avatar_url"] = avatar_url
    if github_username is not None:
        updates["github_username"] = github_username.strip() or None
    if profile_visibility is not None:
        updates["profile_visibility"] = _normalize_visibility(
            profile_visibility, PROFILE_VISIBILITIES, user["profile_visibility"]
        )
    if default_session_visibility is not None:
        updates["default_session_visibility"] = _normalize_visibility(
            default_session_visibility,
            SESSION_VISIBILITIES,
            user["default_session_visibility"],
        )

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    updates["username"] = user["username"]
    conn.execute(f"UPDATE users SET {assignments} WHERE username = :username", updates)
    return get_user_by_identifier(conn, user["username"])


def resolve_session_id(conn: sqlite3.Connection, session_id_prefix: str) -> str | None:
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE session_id = ? LIMIT 1",
        (session_id_prefix,),
    ).fetchone()
    if row:
        return row["session_id"]

    rows = conn.execute(
        """
        SELECT session_id
        FROM sessions
        WHERE session_id LIKE ?
        ORDER BY LENGTH(session_id), session_id
        LIMIT 2
        """,
        (f"{session_id_prefix}%",),
    ).fetchall()
    if not rows:
        return None
    if len(rows) > 1:
        raise ValueError(
            f"Ambiguous session id prefix '{session_id_prefix}'. Use a longer session id."
        )
    return rows[0]["session_id"]


def set_session_visibility(
    conn: sqlite3.Connection,
    session_id_prefix: str,
    visibility: str,
    *,
    shared_dir: Path,
) -> int:
    normalized = _normalize_visibility(visibility, SESSION_VISIBILITIES, "public")
    session_id = resolve_session_id(conn, session_id_prefix)
    if not session_id:
        return 0
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    if normalized == "private":
        from .sync import prepare_private_session_storage

        transition = prepare_private_session_storage(row, shared_dir=Path(shared_dir))
    else:
        from .sync import prepare_shared_session_storage

        transition = prepare_shared_session_storage(row, shared_dir=Path(shared_dir))
    try:
        cur = conn.execute(
            """
            UPDATE sessions
            SET visibility = ?,
                visibility_source = 'manual',
                visibility_rule_id = NULL,
                visibility_reason = 'manual override',
                is_private = CASE WHEN ? = 'private' THEN 1 ELSE 0 END,
                shared_path = ?
            WHERE session_id = ?
            """,
            (
                normalized,
                normalized,
                str(transition.archive_path),
                session_id,
            ),
        )
    except BaseException:
        transition.rollback()
        raise
    defer_storage_transition(conn, transition)
    return cur.rowcount


def create_visibility_rule(
    conn: sqlite3.Connection,
    identifier: str,
    *,
    field: str,
    match_mode: str,
    pattern: str,
    visibility: str,
    priority: int = 100,
    threshold: float | None = None,
    source_scope: str | None = None,
    enabled: bool = True,
):
    user = get_user_by_identifier(conn, identifier)
    if not user:
        return None

    now = _now_iso()
    normalized_field = _normalize_rule_field(field)
    normalized_mode = _normalize_rule_mode(match_mode)
    normalized_visibility = _normalize_visibility(visibility, SESSION_VISIBILITIES, "public")
    normalized_source_scope = _normalize_source_scope(source_scope)
    normalized_threshold = _normalize_threshold(normalized_mode, threshold)

    cur = conn.execute(
        """
        INSERT INTO session_visibility_rules (
            username,
            source_scope,
            field,
            match_mode,
            pattern,
            visibility,
            priority,
            threshold,
            enabled,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user["username"],
            normalized_source_scope,
            normalized_field,
            normalized_mode,
            pattern.strip(),
            normalized_visibility,
            int(priority),
            normalized_threshold,
            1 if enabled else 0,
            now,
            now,
        ),
    )
    return conn.execute(
        """
        SELECT r.*, u.username, COALESCE(u.display_name, u.username) AS user_display_name
        FROM session_visibility_rules r
        JOIN users u ON u.username = r.username
        WHERE r.id = ?
        """,
        (cur.lastrowid,),
    ).fetchone()


def list_visibility_rules(conn: sqlite3.Connection, identifier: str | None = None):
    params: list[str] = []
    where = ""
    if identifier:
        user = get_user_by_identifier(conn, identifier)
        if not user:
            return []
        where = "WHERE r.username = ?"
        params.append(user["username"])

    return conn.execute(
        f"""
        SELECT
            r.*,
            u.username,
            COALESCE(u.display_name, u.username) AS user_display_name
        FROM session_visibility_rules r
        JOIN users u ON u.username = r.username
        {where}
        ORDER BY r.username, r.enabled DESC, r.priority ASC, r.id ASC
        """,
        params,
    ).fetchall()


def delete_visibility_rule(
    conn: sqlite3.Connection,
    rule_id: int,
    *,
    shared_dir: Path,
) -> tuple[int, int]:
    row = conn.execute(
        "SELECT username FROM session_visibility_rules WHERE id = ?",
        (rule_id,),
    ).fetchone()
    cur = conn.execute("DELETE FROM session_visibility_rules WHERE id = ?", (rule_id,))
    if not cur.rowcount or not row:
        return cur.rowcount, 0
    updated = recompute_session_visibility(
        conn,
        identifier=row["username"],
        shared_dir=Path(shared_dir),
    )
    return cur.rowcount, updated


def _session_rule_context(data: dict) -> dict[str, str]:
    return {
        "project": data.get("project") or "",
        "source_path": data.get("source_path") or "",
        "first_user_message": data.get("first_user_message") or "",
        "model": data.get("model") or "",
        "machine": data.get("machine") or "",
        "username": data.get("username") or "",
    }


def resolve_session_visibility(
    conn: sqlite3.Connection,
    *,
    username: str,
    source: str,
    default_visibility: str,
    session_data: dict,
) -> dict[str, str | int | None]:
    rules = conn.execute(
        """
        SELECT *
        FROM session_visibility_rules
        WHERE username = ? AND enabled = 1
        ORDER BY priority ASC, id ASC
        """,
        (username,),
    ).fetchall()
    context = _session_rule_context(session_data)

    for rule in rules:
        if rule["source_scope"] and rule["source_scope"] != source:
            continue
        score = _rule_match_score(
            context.get(rule["field"], ""),
            pattern=rule["pattern"],
            match_mode=rule["match_mode"],
            threshold=rule["threshold"],
        )
        if score is None:
            continue

        reason = f"rule:{rule['id']} {rule['field']} {rule['match_mode']}"
        if rule["source_scope"]:
            reason += f" source={rule['source_scope']}"
        if rule["match_mode"] == "fuzzy":
            reason += f" score={score:.2f}"

        return {
            "visibility": rule["visibility"],
            "visibility_source": "rule",
            "visibility_rule_id": rule["id"],
            "visibility_reason": reason,
        }

    normalized_default = _normalize_visibility(default_visibility, SESSION_VISIBILITIES, "unlisted")
    return {
        "visibility": normalized_default,
        "visibility_source": "default",
        "visibility_rule_id": None,
        "visibility_reason": f"default:{normalized_default}",
    }


def recompute_session_visibility(
    conn: sqlite3.Connection,
    *,
    shared_dir: Path,
    identifier: str | None = None,
    session_id_prefix: str | None = None,
    include_manual: bool = False,
) -> int:
    clauses: list[str] = []
    params: list[str] = []

    if identifier:
        user = get_user_by_identifier(conn, identifier)
        if not user:
            return 0
        clauses.append("s.username = ?")
        params.append(user["username"])
    if session_id_prefix:
        clauses.append("s.session_id LIKE ?")
        params.append(f"{session_id_prefix}%")

    where = " AND ".join(clauses) if clauses else "1 = 1"
    rows = conn.execute(
        f"""
        SELECT
            s.*,
            COALESCE(u.default_session_visibility, 'unlisted') AS default_session_visibility
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE {where}
        ORDER BY s.first_timestamp, s.session_id
        """,
        params,
    ).fetchall()

    updated = 0
    for row in rows:
        if row["visibility_source"] == "manual" and not include_manual:
            continue

        decision = resolve_session_visibility(
            conn,
            username=row["username"],
            source=row["source"],
            default_visibility=row["default_session_visibility"],
            session_data=dict(row),
        )
        conn.execute(
            """
            UPDATE sessions
            SET visibility = ?,
                visibility_source = ?,
                visibility_rule_id = ?,
                visibility_reason = ?,
                is_private = CASE WHEN ? = 'private' THEN 1 ELSE 0 END
            WHERE session_id = ?
            """,
            (
                decision["visibility"],
                decision["visibility_source"],
                decision["visibility_rule_id"],
                decision["visibility_reason"],
                decision["visibility"],
                row["session_id"],
            ),
        )
        updated += 1

    if rows:
        from .sync import reconcile_session_storage

        reconcile_session_storage(
            conn,
            shared_dir=Path(shared_dir),
            session_id_prefix=session_id_prefix,
            username=user["username"] if identifier else None,
        )

    return updated


def preview_session_visibility(conn: sqlite3.Connection, session_id_prefix: str):
    row = conn.execute(
        """
        SELECT
            s.*,
            COALESCE(u.default_session_visibility, 'unlisted') AS default_session_visibility,
            u.username AS canonical_username
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.session_id LIKE ?
        ORDER BY LENGTH(s.session_id), s.session_id
        LIMIT 1
        """,
        (f"{session_id_prefix}%",),
    ).fetchone()
    if not row:
        return None

    decision = resolve_session_visibility(
        conn,
        username=row["username"],
        source=row["source"],
        default_visibility=row["default_session_visibility"],
        session_data=dict(row),
    )
    return {"session": row, "decision": decision}


def _backfill_users(conn: sqlite3.Connection) -> None:
    usernames = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT username FROM sessions WHERE username IS NOT NULL AND username != ''"
        ).fetchall()
    ]
    for username in usernames:
        ensure_user(conn, username)


def _rebuild_identity_schema(conn: sqlite3.Connection) -> None:
    user_columns = _table_columns(conn, "users")
    session_columns = _table_columns(conn, "sessions")
    rule_columns = _table_columns(conn, "session_visibility_rules")
    needs_rebuild = (
        "slug" in user_columns
        or "user_slug" in session_columns
        or "user_slug" in rule_columns
    )
    if not needs_rebuild:
        return

    now = _now_iso()
    conn.execute("DROP VIEW IF EXISTS session_catalog")
    conn.execute("DROP VIEW IF EXISTS user_catalog")

    conn.execute(
        """
        CREATE TABLE users__new (
            username TEXT PRIMARY KEY,
            display_name TEXT,
            bio TEXT,
            avatar_url TEXT,
            profile_visibility TEXT NOT NULL DEFAULT 'public',
            default_session_visibility TEXT NOT NULL DEFAULT 'unlisted',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if "slug" in user_columns:
        conn.execute(
            """
            INSERT INTO users__new (
                username, display_name, bio, avatar_url,
                profile_visibility, default_session_visibility,
                created_at, updated_at
            )
            SELECT
                normalize_username_py(COALESCE(NULLIF(username, ''), slug)) AS username,
                display_name,
                bio,
                avatar_url,
                COALESCE(NULLIF(profile_visibility, ''), 'public') AS profile_visibility,
                CASE
                    WHEN default_session_visibility IN ('private', 'unlisted', 'public')
                    THEN default_session_visibility
                    ELSE 'unlisted'
                END AS default_session_visibility,
                COALESCE(NULLIF(created_at, ''), ?) AS created_at,
                COALESCE(NULLIF(updated_at, ''), ?) AS updated_at
            FROM users
            """,
            (now, now),
        )
    else:
        conn.execute(
            """
            INSERT INTO users__new (
                username, display_name, bio, avatar_url,
                profile_visibility, default_session_visibility,
                created_at, updated_at
            )
            SELECT
                normalize_username_py(username) AS username,
                display_name,
                bio,
                avatar_url,
                COALESCE(NULLIF(profile_visibility, ''), 'public') AS profile_visibility,
                CASE
                    WHEN default_session_visibility IN ('private', 'unlisted', 'public')
                    THEN default_session_visibility
                    ELSE 'unlisted'
                END AS default_session_visibility,
                COALESCE(NULLIF(created_at, ''), ?) AS created_at,
                COALESCE(NULLIF(updated_at, ''), ?) AS updated_at
            FROM users
            """,
            (now, now),
        )
    conn.execute(
        """
        INSERT OR IGNORE INTO users__new (
            username, display_name, bio, avatar_url,
            profile_visibility, default_session_visibility,
            created_at, updated_at
        )
        SELECT DISTINCT
            normalize_username_py(username) AS username,
            normalize_username_py(username) AS display_name,
            NULL,
            NULL,
            'public',
            'unlisted',
            ?,
            ?
        FROM sessions
        WHERE username IS NOT NULL AND username != ''
        """,
        (now, now),
    )

    conn.execute(
        """
        CREATE TABLE session_visibility_rules__new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            source_scope TEXT,
            field TEXT NOT NULL,
            match_mode TEXT NOT NULL,
            pattern TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'public',
            priority INTEGER NOT NULL DEFAULT 100,
            threshold REAL,
            enabled INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    if "user_slug" in rule_columns:
        conn.execute(
            """
            INSERT INTO session_visibility_rules__new (
                id, username, source_scope, field, match_mode, pattern,
                visibility, priority, threshold, enabled, created_at, updated_at
            )
            SELECT
                r.id,
                COALESCE(
                    (SELECT u.username FROM users__new u WHERE u.username = normalize_username_py(r.user_slug)),
                    normalize_username_py(r.user_slug)
                ) AS username,
                r.source_scope,
                r.field,
                r.match_mode,
                r.pattern,
                r.visibility,
                r.priority,
                r.threshold,
                r.enabled,
                COALESCE(NULLIF(r.created_at, ''), ?),
                COALESCE(NULLIF(r.updated_at, ''), ?)
            FROM session_visibility_rules r
            """,
            (now, now),
        )
    else:
        conn.execute(
            """
            INSERT INTO session_visibility_rules__new (
                id, username, source_scope, field, match_mode, pattern,
                visibility, priority, threshold, enabled, created_at, updated_at
            )
            SELECT
                id,
                normalize_username_py(username) AS username,
                source_scope,
                field,
                match_mode,
                pattern,
                visibility,
                priority,
                threshold,
                enabled,
                COALESCE(NULLIF(created_at, ''), ?),
                COALESCE(NULLIF(updated_at, ''), ?)
            FROM session_visibility_rules
            """,
            (now, now),
        )

    conn.execute(
        """
        CREATE TABLE sessions__new (
            session_id     TEXT PRIMARY KEY,
            source         TEXT NOT NULL,
            username       TEXT NOT NULL,
            machine        TEXT,
            project        TEXT,
            workspace_root TEXT,
            worktree_root  TEXT,
            repo_root      TEXT,
            repo_name      TEXT,
            git_branch     TEXT,
            git_commit     TEXT,
            git_dirty      INTEGER DEFAULT 0,
            source_path    TEXT NOT NULL,
            shared_path    TEXT NOT NULL,
            first_timestamp TEXT,
            last_timestamp  TEXT,
            duration_seconds REAL,
            user_message_count    INTEGER DEFAULT 0,
            assistant_message_count INTEGER DEFAULT 0,
            tool_call_count       INTEGER DEFAULT 0,
            error_count           INTEGER DEFAULT 0,
            write_path_count      INTEGER,
            read_path_count       INTEGER,
            search_path_count     INTEGER,
            test_run_count        INTEGER,
            test_failure_count    INTEGER,
            lint_run_count        INTEGER,
            lint_failure_count    INTEGER,
            build_run_count       INTEGER,
            build_failure_count   INTEGER,
            format_run_count      INTEGER,
            format_failure_count  INTEGER,
            git_status_count      INTEGER,
            git_diff_count        INTEGER,
            git_commit_count      INTEGER,
            activity_version      INTEGER DEFAULT 0,
            session_goal          TEXT,
            session_summary       TEXT,
            session_outcome       TEXT,
            session_status        TEXT,
            narrative_version     INTEGER DEFAULT 0,
            objective_family      TEXT,
            objective_label       TEXT,
            objective_version     INTEGER DEFAULT 0,
            session_origin        TEXT,
            origin_version        INTEGER DEFAULT 0,
            total_input_tokens    INTEGER DEFAULT 0,
            total_output_tokens   INTEGER DEFAULT 0,
            fresh_input_tokens    INTEGER DEFAULT 0,
            cached_input_tokens   INTEGER DEFAULT 0,
            reasoning_output_tokens INTEGER DEFAULT 0,
            token_version        INTEGER DEFAULT 0,
            first_user_message    TEXT,
            parent_session_id    TEXT,
            spawn_depth          INTEGER DEFAULT 0,
            visibility     TEXT NOT NULL DEFAULT 'public',
            visibility_source TEXT NOT NULL DEFAULT 'default',
            visibility_rule_id INTEGER,
            visibility_reason TEXT,
            is_private     INTEGER DEFAULT 0,
            file_hash      TEXT,
            synced_at      TEXT,
            model          TEXT
        )
        """
    )
    username_expr = (
        "COALESCE(NULLIF(user_slug, ''), NULLIF(username, ''))"
        if "user_slug" in session_columns
        else "NULLIF(username, '')"
    )
    conn.execute(
        f"""
        INSERT INTO sessions__new (
            session_id, source, username, machine, project, workspace_root,
            worktree_root, repo_root, repo_name, git_branch, git_commit, git_dirty,
            source_path, shared_path, first_timestamp, last_timestamp, duration_seconds,
            user_message_count, assistant_message_count, tool_call_count, error_count,
            write_path_count, read_path_count, search_path_count,
            test_run_count, test_failure_count, lint_run_count, lint_failure_count,
            build_run_count, build_failure_count, format_run_count, format_failure_count,
            git_status_count, git_diff_count, git_commit_count, activity_version,
            session_goal, session_summary, session_outcome, session_status, narrative_version,
            objective_family, objective_label, objective_version,
            session_origin, origin_version,
            total_input_tokens, total_output_tokens, fresh_input_tokens, cached_input_tokens,
            reasoning_output_tokens, token_version, first_user_message, parent_session_id, spawn_depth, visibility,
            visibility_source, visibility_rule_id, visibility_reason,
            is_private, file_hash, synced_at, model
        )
        SELECT
            session_id,
            source,
            COALESCE(
                (SELECT u.username FROM users__new u WHERE u.username = normalize_username_py({username_expr})),
                normalize_username_py({username_expr}),
                'user'
            ) AS username,
            machine,
            project,
            workspace_root,
            worktree_root,
            repo_root,
            repo_name,
            git_branch,
            git_commit,
            COALESCE(git_dirty, 0),
            source_path,
            shared_path,
            first_timestamp,
            last_timestamp,
            duration_seconds,
            COALESCE(user_message_count, 0),
            COALESCE(assistant_message_count, 0),
            COALESCE(tool_call_count, 0),
            COALESCE(error_count, 0),
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
            COALESCE(activity_version, 0),
            session_goal,
            session_summary,
            session_outcome,
            session_status,
            COALESCE(narrative_version, 0),
            objective_family,
            objective_label,
            COALESCE(objective_version, 0),
            session_origin,
            COALESCE(origin_version, 0),
            COALESCE(total_input_tokens, 0),
            COALESCE(total_output_tokens, 0),
            COALESCE(fresh_input_tokens, 0),
            COALESCE(cached_input_tokens, 0),
            COALESCE(reasoning_output_tokens, 0),
            COALESCE(token_version, 0),
            first_user_message,
            parent_session_id,
            COALESCE(spawn_depth, 0),
            COALESCE(NULLIF(visibility, ''), 'public'),
            COALESCE(NULLIF(visibility_source, ''), 'default'),
            visibility_rule_id,
            visibility_reason,
            COALESCE(is_private, 0),
            file_hash,
            synced_at,
            model
        FROM sessions
        """
    )

    conn.execute("DROP TABLE session_visibility_rules")
    conn.execute("ALTER TABLE session_visibility_rules__new RENAME TO session_visibility_rules")
    conn.execute("DROP TABLE sessions")
    conn.execute("ALTER TABLE sessions__new RENAME TO sessions")
    conn.execute("DROP TABLE users")
    conn.execute("ALTER TABLE users__new RENAME TO users")


def migrate_db(conn: sqlite3.Connection) -> None:
    conn.create_function("normalize_username_py", 1, lambda value: normalize_username(value or ""))
    conn.executescript(SCHEMA)

    _ensure_column(conn, "sessions", "workspace_root", "TEXT")
    _ensure_column(conn, "sessions", "worktree_root", "TEXT")
    _ensure_column(conn, "sessions", "repo_root", "TEXT")
    _ensure_column(conn, "sessions", "repo_name", "TEXT")
    _ensure_column(conn, "sessions", "git_branch", "TEXT")
    _ensure_column(conn, "sessions", "git_commit", "TEXT")
    _ensure_column(conn, "sessions", "git_dirty", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "write_path_count", "INTEGER")
    _ensure_column(conn, "sessions", "read_path_count", "INTEGER")
    _ensure_column(conn, "sessions", "search_path_count", "INTEGER")
    _ensure_column(conn, "sessions", "test_run_count", "INTEGER")
    _ensure_column(conn, "sessions", "test_failure_count", "INTEGER")
    _ensure_column(conn, "sessions", "lint_run_count", "INTEGER")
    _ensure_column(conn, "sessions", "lint_failure_count", "INTEGER")
    _ensure_column(conn, "sessions", "build_run_count", "INTEGER")
    _ensure_column(conn, "sessions", "build_failure_count", "INTEGER")
    _ensure_column(conn, "sessions", "format_run_count", "INTEGER")
    _ensure_column(conn, "sessions", "format_failure_count", "INTEGER")
    _ensure_column(conn, "sessions", "git_status_count", "INTEGER")
    _ensure_column(conn, "sessions", "git_diff_count", "INTEGER")
    _ensure_column(conn, "sessions", "git_commit_count", "INTEGER")
    _ensure_column(conn, "sessions", "activity_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "session_goal", "TEXT")
    _ensure_column(conn, "sessions", "session_summary", "TEXT")
    _ensure_column(conn, "sessions", "session_outcome", "TEXT")
    _ensure_column(conn, "sessions", "session_status", "TEXT")
    _ensure_column(conn, "sessions", "narrative_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "objective_family", "TEXT")
    _ensure_column(conn, "sessions", "objective_label", "TEXT")
    _ensure_column(conn, "sessions", "objective_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "session_origin", "TEXT")
    _ensure_column(conn, "sessions", "origin_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "fresh_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "cached_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "reasoning_output_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "token_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "parent_session_id", "TEXT")
    _ensure_column(conn, "sessions", "spawn_depth", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "visibility", "TEXT NOT NULL DEFAULT 'public'")
    _ensure_column(conn, "sessions", "visibility_source", "TEXT NOT NULL DEFAULT 'default'")
    _ensure_column(conn, "sessions", "visibility_rule_id", "INTEGER")
    _ensure_column(conn, "sessions", "visibility_reason", "TEXT")
    _ensure_column(conn, "session_paths", "repo_relative_path", "TEXT")
    _ensure_column(conn, "users", "display_name", "TEXT")
    _ensure_column(conn, "users", "bio", "TEXT")
    _ensure_column(conn, "users", "avatar_url", "TEXT")
    _ensure_column(conn, "users", "profile_visibility", "TEXT NOT NULL DEFAULT 'public'")
    _ensure_column(
        conn,
        "users",
        "default_session_visibility",
        "TEXT NOT NULL DEFAULT 'unlisted'",
    )
    _ensure_column(conn, "users", "created_at", "TEXT")
    _ensure_column(conn, "users", "updated_at", "TEXT")
    _ensure_column(conn, "users", "github_username", "TEXT")

    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS user_github_daily (
            username TEXT NOT NULL,
            day TEXT NOT NULL,
            contributions INTEGER DEFAULT 0,
            commits INTEGER DEFAULT 0,
            prs_opened INTEGER DEFAULT 0,
            prs_reviewed INTEGER DEFAULT 0,
            issues_opened INTEGER DEFAULT 0,
            synced_at TEXT NOT NULL,
            PRIMARY KEY (username, day)
        );
        CREATE INDEX IF NOT EXISTS idx_user_github_day ON user_github_daily(day);
        CREATE INDEX IF NOT EXISTS idx_user_github_username ON user_github_daily(username);
        """
    )

    _backfill_users(conn)
    _rebuild_identity_schema(conn)
    _backfill_users(conn)
    # After _rebuild_identity_schema: the rebuild recreates `sessions` from a
    # fixed column list that predates these columns, so adding them earlier
    # would be undone on legacy-slug databases.
    _ensure_column(conn, "sessions", "cache_creation_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "cache_creation_5m_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "cache_creation_1h_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "file_size", "INTEGER")
    _ensure_column(conn, "sessions", "file_mtime", "REAL")
    for native_column, _transcript_column in NATIVE_TOKEN_COLUMNS:
        _ensure_column(conn, "sessions", native_column, "INTEGER DEFAULT 0")
        _ensure_column(conn, "session_daily_usage", native_column, "INTEGER NOT NULL DEFAULT 0")
    # Claims whose owning session left the ledger no longer feed any native
    # aggregate; drop them. (Duplicates in other transcripts re-claim on
    # their next re-parse, not automatically.)
    conn.execute(
        "DELETE FROM message_claims WHERE owner_session_id NOT IN (SELECT session_id FROM sessions)"
    )
    # Give pre-claims rows sane native values immediately (mirror transcript
    # totals) so aggregate readers never see zeros between the column
    # migration and the first full re-parse.
    _refresh_native_mirror(conn)
    conn.execute(
        """
        UPDATE sessions
        SET workspace_root = CASE
            WHEN workspace_root IS NOT NULL AND workspace_root != '' THEN workspace_root
            WHEN project LIKE '/%' OR project LIKE '~/%' THEN project
            ELSE workspace_root
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET worktree_root = CASE
            WHEN worktree_root IS NOT NULL AND worktree_root != '' THEN worktree_root
            WHEN workspace_root IS NOT NULL AND workspace_root != '' THEN workspace_root
            ELSE worktree_root
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET git_dirty = CASE
            WHEN git_dirty IS NULL THEN 0
            ELSE git_dirty
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET activity_version = CASE
            WHEN activity_version IS NULL THEN 0
            ELSE activity_version
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET narrative_version = CASE
            WHEN narrative_version IS NULL THEN 0
            ELSE narrative_version
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET objective_version = CASE
            WHEN objective_version IS NULL THEN 0
            ELSE objective_version
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET origin_version = CASE
            WHEN origin_version IS NULL THEN 0
            ELSE origin_version
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET session_status = CASE
            WHEN session_status IN ('exploration', 'success', 'partial', 'failed')
            THEN session_status
            ELSE 'exploration'
        END
        """
    )
    conn.execute(
        f"""
        UPDATE sessions
        SET session_origin = CASE
            WHEN session_origin IN ({", ".join(repr(origin) for origin in SESSION_ORIGINS)})
            THEN session_origin
            ELSE 'human_direct'
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET visibility = CASE
            WHEN is_private = 1 THEN 'private'
            WHEN visibility IS NULL OR visibility = '' THEN 'public'
            ELSE visibility
        END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET is_private = CASE WHEN visibility = 'private' THEN 1 ELSE 0 END
        """
    )
    conn.execute(
        """
        UPDATE sessions
        SET visibility_source = CASE
                WHEN visibility_source IN ('manual', 'rule', 'default') THEN visibility_source
                WHEN visibility IN ('private', 'unlisted') OR is_private = 1 THEN 'manual'
                ELSE 'default'
            END,
            visibility_rule_id = CASE
                WHEN visibility_source = 'rule' THEN visibility_rule_id
                ELSE NULL
            END,
            visibility_reason = CASE
                WHEN visibility_reason IS NOT NULL AND visibility_reason != '' THEN visibility_reason
                WHEN visibility IN ('private', 'unlisted') OR is_private = 1 THEN 'legacy manual'
                ELSE 'legacy default'
            END
        """
    )
    conn.execute(
        """
        UPDATE users
        SET display_name = COALESCE(NULLIF(display_name, ''), username),
            profile_visibility = CASE
                WHEN profile_visibility IN ('private', 'unlisted', 'public') THEN profile_visibility
                ELSE 'public'
            END,
            default_session_visibility = CASE
                WHEN default_session_visibility IN ('private', 'unlisted', 'public')
                THEN default_session_visibility
                ELSE 'unlisted'
            END,
            created_at = COALESCE(NULLIF(created_at, ''), ?),
            updated_at = COALESCE(NULLIF(updated_at, ''), ?)
        """,
        (_now_iso(), _now_iso()),
    )
    conn.execute(
        """
        UPDATE sessions
        SET username = normalize_username_py(username)
        """
    )
    conn.executescript(INDEXES)
    conn.executescript(VIEWS)


class _StorageTransactionConnection(sqlite3.Connection):
    """SQLite connection that commits filesystem transitions with the DB.

    A visibility change prepares reversible filesystem operations before it
    updates SQLite.  Keeping those operations registered on the connection
    means an explicit commit (including the periodic commits used by sync)
    finalizes them only after SQLite has durably committed, while any rollback
    restores the previous storage layout.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._storage_transitions: list[object] = []

    def defer_storage_transition(self, transition: object) -> None:
        self._storage_transitions.append(transition)

    def _take_storage_transitions(self) -> list[object]:
        transitions = self._storage_transitions
        self._storage_transitions = []
        return transitions

    @staticmethod
    def _finish_storage_transitions(
        transitions: list[object], method: str, *, reverse: bool = False
    ) -> None:
        first_error: BaseException | None = None
        ordered = reversed(transitions) if reverse else transitions
        for transition in ordered:
            try:
                getattr(transition, method)()
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
        if first_error is not None:
            raise first_error

    def _commit_database(self) -> None:
        """Separate seam for deterministic commit-failure tests."""
        super().commit()

    def commit(self) -> None:
        try:
            self._commit_database()
        except BaseException:
            transitions = self._take_storage_transitions()
            try:
                super().rollback()
            finally:
                self._finish_storage_transitions(
                    transitions, "rollback", reverse=True
                )
            raise

        transitions = self._take_storage_transitions()
        self._finish_storage_transitions(transitions, "commit")

    def rollback(self) -> None:
        transitions = self._take_storage_transitions()
        try:
            super().rollback()
        finally:
            self._finish_storage_transitions(
                transitions, "rollback", reverse=True
            )


def defer_storage_transitions(
    conn: sqlite3.Connection, transitions: list[object]
) -> None:
    """Bind reversible filesystem transitions to one SQLite commit.

    Logpile's managed connections defer cleanup until their next durable
    commit. A raw sqlite3 connection cannot expose commit hooks, so commit the
    complete prepared batch here before finalizing any filesystem cleanup.
    """
    if not transitions:
        return
    defer = getattr(conn, "defer_storage_transition", None)
    if defer is not None:
        for transition in transitions:
            defer(transition)
        return

    try:
        conn.commit()
    except BaseException:
        try:
            conn.rollback()
        finally:
            first_error: BaseException | None = None
            for transition in reversed(transitions):
                try:
                    transition.rollback()
                except BaseException as exc:
                    if first_error is None:
                        first_error = exc
            if first_error is not None:
                raise first_error
        raise
    for transition in transitions:
        transition.commit()


def defer_storage_transition(conn: sqlite3.Connection, transition: object) -> None:
    """Bind one reversible filesystem transition to a SQLite transaction."""
    defer_storage_transitions(conn, [transition])


@contextmanager
def get_db(db_path: Path):
    db_path = Path(db_path)
    if not db_path.exists():
        try:
            fd = os.open(db_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
    os.chmod(db_path, 0o600)
    conn = sqlite3.connect(db_path, factory=_StorageTransactionConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    def harden_database_files() -> None:
        for path in (
            db_path,
            Path(f"{db_path}-wal"),
            Path(f"{db_path}-shm"),
        ):
            if path.exists() and not path.is_symlink():
                path.chmod(0o600)

    harden_database_files()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()
    finally:
        harden_database_files()
        conn.close()
        harden_database_files()


def init_db(db_path: Path):
    db_path = Path(db_path)
    if not db_path.parent.exists():
        missing: list[Path] = []
        cursor = db_path.parent
        while not cursor.exists():
            missing.append(cursor)
            cursor = cursor.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700)
    unsafe_shared_parents = {
        Path("/"),
        Path("/tmp"),
        Path("/private/tmp"),
        Path.home(),
    }
    if db_path.parent not in unsafe_shared_parents and not db_path.parent.is_symlink():
        db_path.parent.chmod(0o700)
    with get_db(db_path) as conn:
        migrate_db(conn)


def upsert_session(conn, data: dict):
    payload = {
        "session_goal": None,
        "session_summary": None,
        "session_outcome": None,
        "session_status": "exploration",
        "narrative_version": 0,
        "objective_family": None,
        "objective_label": None,
        "objective_version": 0,
        "session_origin": "human_direct",
        "origin_version": 0,
        "fresh_input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "cache_creation_5m_input_tokens": 0,
        "cache_creation_1h_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "token_version": 0,
        "parent_session_id": None,
        "spawn_depth": 0,
        "file_size": None,
        "file_mtime": None,
        **data,
    }
    if payload.get("username"):
        payload["username"] = normalize_username(str(payload["username"]))
    conn.execute(
        """
        INSERT INTO sessions
            (session_id, source, username, machine, project, workspace_root,
             worktree_root, repo_root, repo_name, git_branch, git_commit, git_dirty,
             source_path, shared_path, first_timestamp, last_timestamp,
             duration_seconds, user_message_count, assistant_message_count,
             tool_call_count, error_count, write_path_count, read_path_count, search_path_count,
             test_run_count, test_failure_count, lint_run_count, lint_failure_count,
             build_run_count, build_failure_count, format_run_count, format_failure_count,
             git_status_count, git_diff_count, git_commit_count, activity_version,
             session_goal, session_summary, session_outcome, session_status, narrative_version,
             objective_family, objective_label, objective_version,
             session_origin, origin_version,
             total_input_tokens, total_output_tokens, fresh_input_tokens, cached_input_tokens,
             cache_creation_input_tokens, cache_creation_5m_input_tokens, cache_creation_1h_input_tokens,
             reasoning_output_tokens, token_version, first_user_message, parent_session_id, spawn_depth, visibility,
             visibility_source, visibility_rule_id, visibility_reason,
             is_private, file_hash, file_size, file_mtime, synced_at, model)
        VALUES
            (:session_id, :source, :username, :machine, :project, :workspace_root,
             :worktree_root, :repo_root, :repo_name, :git_branch, :git_commit, :git_dirty,
             :source_path, :shared_path, :first_timestamp, :last_timestamp,
             :duration_seconds, :user_message_count, :assistant_message_count,
             :tool_call_count, :error_count, :write_path_count, :read_path_count, :search_path_count,
             :test_run_count, :test_failure_count, :lint_run_count, :lint_failure_count,
             :build_run_count, :build_failure_count, :format_run_count, :format_failure_count,
             :git_status_count, :git_diff_count, :git_commit_count, :activity_version,
             :session_goal, :session_summary, :session_outcome, :session_status, :narrative_version,
             :objective_family, :objective_label, :objective_version,
             :session_origin, :origin_version,
             :total_input_tokens, :total_output_tokens, :fresh_input_tokens, :cached_input_tokens,
             :cache_creation_input_tokens, :cache_creation_5m_input_tokens, :cache_creation_1h_input_tokens,
             :reasoning_output_tokens, :token_version, :first_user_message, :parent_session_id, :spawn_depth, :visibility,
             :visibility_source, :visibility_rule_id, :visibility_reason,
             :is_private, :file_hash, :file_size, :file_mtime, :synced_at, :model)
        ON CONFLICT(session_id) DO UPDATE SET
            source = excluded.source,
            username = excluded.username,
            machine = excluded.machine,
            project = excluded.project,
            workspace_root = excluded.workspace_root,
            worktree_root = excluded.worktree_root,
            repo_root = excluded.repo_root,
            repo_name = excluded.repo_name,
            git_branch = excluded.git_branch,
            git_commit = excluded.git_commit,
            git_dirty = excluded.git_dirty,
            source_path = excluded.source_path,
            shared_path = excluded.shared_path,
            first_timestamp = excluded.first_timestamp,
            last_timestamp = excluded.last_timestamp,
            duration_seconds = excluded.duration_seconds,
            user_message_count = excluded.user_message_count,
            assistant_message_count = excluded.assistant_message_count,
            tool_call_count = excluded.tool_call_count,
            error_count = excluded.error_count,
            write_path_count = excluded.write_path_count,
            read_path_count = excluded.read_path_count,
            search_path_count = excluded.search_path_count,
            test_run_count = excluded.test_run_count,
            test_failure_count = excluded.test_failure_count,
            lint_run_count = excluded.lint_run_count,
            lint_failure_count = excluded.lint_failure_count,
            build_run_count = excluded.build_run_count,
            build_failure_count = excluded.build_failure_count,
            format_run_count = excluded.format_run_count,
            format_failure_count = excluded.format_failure_count,
            git_status_count = excluded.git_status_count,
            git_diff_count = excluded.git_diff_count,
            git_commit_count = excluded.git_commit_count,
            activity_version = excluded.activity_version,
            session_goal = excluded.session_goal,
            session_summary = excluded.session_summary,
            session_outcome = excluded.session_outcome,
            session_status = excluded.session_status,
            narrative_version = excluded.narrative_version,
            objective_family = excluded.objective_family,
            objective_label = excluded.objective_label,
            objective_version = excluded.objective_version,
            session_origin = excluded.session_origin,
            origin_version = excluded.origin_version,
            total_input_tokens = excluded.total_input_tokens,
            total_output_tokens = excluded.total_output_tokens,
            fresh_input_tokens = excluded.fresh_input_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cache_creation_input_tokens = excluded.cache_creation_input_tokens,
            cache_creation_5m_input_tokens = excluded.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens = excluded.cache_creation_1h_input_tokens,
            reasoning_output_tokens = excluded.reasoning_output_tokens,
            token_version = excluded.token_version,
            first_user_message = excluded.first_user_message,
            parent_session_id = excluded.parent_session_id,
            spawn_depth = excluded.spawn_depth,
            visibility = CASE
                WHEN COALESCE(NULLIF(sessions.visibility_source, ''), 'default') = 'manual'
                THEN COALESCE(NULLIF(sessions.visibility, ''), excluded.visibility)
                ELSE excluded.visibility
            END,
            visibility_source = CASE
                WHEN COALESCE(NULLIF(sessions.visibility_source, ''), 'default') = 'manual'
                THEN 'manual'
                ELSE excluded.visibility_source
            END,
            visibility_rule_id = CASE
                WHEN COALESCE(NULLIF(sessions.visibility_source, ''), 'default') = 'manual'
                THEN NULL
                ELSE excluded.visibility_rule_id
            END,
            visibility_reason = CASE
                WHEN COALESCE(NULLIF(sessions.visibility_source, ''), 'default') = 'manual'
                THEN COALESCE(NULLIF(sessions.visibility_reason, ''), 'manual override')
                ELSE excluded.visibility_reason
            END,
            is_private = CASE
                WHEN (
                    CASE
                        WHEN COALESCE(NULLIF(sessions.visibility_source, ''), 'default') = 'manual'
                        THEN COALESCE(NULLIF(sessions.visibility, ''), excluded.visibility)
                        ELSE excluded.visibility
                    END
                ) = 'private' THEN 1 ELSE 0
            END,
            file_hash = excluded.file_hash,
            file_size = excluded.file_size,
            file_mtime = excluded.file_mtime,
            synced_at = excluded.synced_at,
            model = excluded.model
        """,
        payload,
    )


def insert_session_daily_usage(conn, session_id: str, daily_usage: list):
    conn.execute("DELETE FROM session_daily_usage WHERE session_id = ?", (session_id,))
    if not daily_usage:
        return
    conn.executemany(
        """
        INSERT INTO session_daily_usage (
            session_id, day,
            total_input_tokens, total_output_tokens,
            fresh_input_tokens, cached_input_tokens,
            cache_creation_input_tokens, cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens, reasoning_output_tokens,
            user_message_count, assistant_message_count, tool_call_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                session_id,
                d.day,
                d.total_input_tokens,
                d.total_output_tokens,
                d.fresh_input_tokens,
                d.cached_input_tokens,
                d.cache_creation_input_tokens,
                d.cache_creation_5m_input_tokens,
                d.cache_creation_1h_input_tokens,
                d.reasoning_output_tokens,
                d.user_message_count,
                d.assistant_message_count,
                d.tool_call_count,
            )
            for d in daily_usage
        ],
    )


def _chunked(values: list, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _session_rank(last_timestamp, first_timestamp, session_id) -> tuple[str, str, str]:
    """Ownership rank; the minimum-ranked claimant owns a message.

    Earliest last_timestamp wins: a resume transcript is a superset of its
    source, so it always ends at or after the source — the earliest-ending
    claimant is the session where the message actually ran. Sessions without
    timestamps rank after any dated session ("~" > every digit in ASCII).
    """
    return (last_timestamp or "~", first_timestamp or "~", session_id)


def apply_message_claims(conn, session_id: str, message_usage: list) -> set[str]:
    """Resolve cross-session ownership for one parsed session's messages.

    Upserts this session's claims into message_claims: new keys are claimed,
    keys it already owns get refreshed values, keys owned by a higher-ranked
    session are stolen, and stale keys it owns but no longer emits are
    deleted. Because ownership is a pure min-rule over current session ranks,
    the final owner set is the same whatever order files are (re)parsed in.

    The session's own row must already be upserted (rank reads sessions).
    Returns the set of session ids whose native aggregates are now stale
    (always includes session_id itself when anything changed).
    """
    my_row = conn.execute(
        "SELECT last_timestamp, first_timestamp FROM sessions WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    my_rank = _session_rank(
        my_row["last_timestamp"] if my_row else None,
        my_row["first_timestamp"] if my_row else None,
        session_id,
    )

    affected: set[str] = set()
    current_keys = {m.claim_key for m in message_usage}

    existing: dict[str, tuple] = {}
    keys = sorted(current_keys)
    for chunk in _chunked(keys):
        placeholders = ",".join("?" * len(chunk))
        for row in conn.execute(
            f"""
            SELECT c.claim_key, c.owner_session_id,
                   s.session_id AS owner_in_ledger,
                   s.last_timestamp, s.first_timestamp,
                   c.day, c.model,
                   c.fresh_input_tokens, c.cached_input_tokens,
                   c.cache_creation_input_tokens, c.cache_creation_5m_input_tokens,
                   c.cache_creation_1h_input_tokens, c.output_tokens
            FROM message_claims c
            LEFT JOIN sessions s ON s.session_id = c.owner_session_id
            WHERE c.claim_key IN ({placeholders})
            """,
            chunk,
        ):
            existing[row["claim_key"]] = row

    to_write = []
    for m in message_usage:
        values = (
            m.day, m.model,
            m.fresh_input_tokens, m.cached_input_tokens,
            m.cache_creation_input_tokens, m.cache_creation_5m_input_tokens,
            m.cache_creation_1h_input_tokens, m.output_tokens,
        )
        row = existing.get(m.claim_key)
        if row is None:
            to_write.append((m.claim_key, session_id) + values)
            affected.add(session_id)
            continue
        owner = row["owner_session_id"]
        if owner != session_id:
            if row["owner_in_ledger"] is None:
                # Owner vanished from the ledger: any live claimant takes over
                # ("~~" outranks even a timestamp-less session's "~").
                owner_rank = ("~~", "~~", "~~")
            else:
                owner_rank = _session_rank(
                    row["last_timestamp"], row["first_timestamp"], owner
                )
            if my_rank < owner_rank:
                to_write.append((m.claim_key, session_id) + values)
                affected.add(session_id)
                affected.add(owner)
            continue
        if (
            row["day"], row["model"],
            row["fresh_input_tokens"], row["cached_input_tokens"],
            row["cache_creation_input_tokens"], row["cache_creation_5m_input_tokens"],
            row["cache_creation_1h_input_tokens"], row["output_tokens"],
        ) != values:
            to_write.append((m.claim_key, session_id) + values)
            affected.add(session_id)

    if to_write:
        conn.executemany(
            """
            INSERT OR REPLACE INTO message_claims (
                claim_key, owner_session_id, day, model,
                fresh_input_tokens, cached_input_tokens,
                cache_creation_input_tokens, cache_creation_5m_input_tokens,
                cache_creation_1h_input_tokens, output_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            to_write,
        )

    # A re-parse can retire keys this session claimed earlier (e.g. the
    # kept-copy of a retried message.id changed requestId). Every transcript
    # holding the message keeps the retired key out of its parse for the same
    # reason, so deleting is safe — no other session is waiting to claim it.
    owned = [
        row[0]
        for row in conn.execute(
            "SELECT claim_key FROM message_claims WHERE owner_session_id = ?",
            (session_id,),
        )
    ]
    stale = [key for key in owned if key not in current_keys]
    if stale:
        for chunk in _chunked(stale):
            placeholders = ",".join("?" * len(chunk))
            conn.execute(
                f"DELETE FROM message_claims WHERE owner_session_id = ? AND claim_key IN ({placeholders})",
                [session_id, *chunk],
            )
        affected.add(session_id)

    return affected


def _native_scope_clause(session_ids: set[str] | list[str] | None) -> tuple[str, list]:
    if session_ids is None:
        return "", []
    ids = sorted(session_ids)
    placeholders = ",".join("?" * len(ids))
    return f" AND session_id IN ({placeholders})", ids


def _refresh_native_mirror(conn, session_ids=None) -> None:
    """native_* = transcript totals for rows not governed by claims:
    codex (parse-time replay handling already makes totals live-only) and
    claudecode rows below CLAIMS_TOKEN_VERSION (no re-parseable bytes)."""
    assignments = ", ".join(f"{native} = {transcript}" for native, transcript in NATIVE_TOKEN_COLUMNS)
    drift_guard = " OR ".join(f"{native} != {transcript}" for native, transcript in NATIVE_TOKEN_COLUMNS)
    scope, params = _native_scope_clause(session_ids)
    predicate = (
        "session_id IN (SELECT session_id FROM sessions "
        " WHERE source != 'claudecode' OR COALESCE(token_version, 0) < ?)"
    )
    conn.execute(
        f"UPDATE sessions SET {assignments} WHERE {predicate}{scope} AND ({drift_guard})",
        [CLAIMS_TOKEN_VERSION, *params],
    )
    conn.execute(
        f"UPDATE session_daily_usage SET {assignments} WHERE {predicate}{scope} AND ({drift_guard})",
        [CLAIMS_TOKEN_VERSION, *params],
    )


def _refresh_native_claims(conn, session_ids=None) -> None:
    """native_* from owned message_claims for claudecode rows parsed at
    CLAIMS_TOKEN_VERSION or later. Claims carry no reasoning tokens (Claude
    reports none), so native_reasoning_output_tokens is 0 here."""
    native_columns = ", ".join(native for native, _ in NATIVE_TOKEN_COLUMNS)
    aggregates = """
        COALESCE(SUM(c.fresh_input_tokens + c.cache_creation_input_tokens + c.cached_input_tokens), 0),
        COALESCE(SUM(c.output_tokens), 0),
        COALESCE(SUM(c.fresh_input_tokens), 0),
        COALESCE(SUM(c.cached_input_tokens), 0),
        COALESCE(SUM(c.cache_creation_input_tokens), 0),
        COALESCE(SUM(c.cache_creation_5m_input_tokens), 0),
        COALESCE(SUM(c.cache_creation_1h_input_tokens), 0),
        0,
        COUNT(c.claim_key)
    """
    scope, params = _native_scope_clause(session_ids)
    conn.execute(
        f"""
        UPDATE sessions SET ({native_columns}) = (
            SELECT {aggregates}
            FROM message_claims c
            WHERE c.owner_session_id = sessions.session_id
        )
        WHERE source = 'claudecode' AND COALESCE(token_version, 0) >= ?{scope}
        """,
        [CLAIMS_TOKEN_VERSION, *params],
    )
    conn.execute(
        f"""
        UPDATE session_daily_usage SET ({native_columns}) = (
            SELECT {aggregates}
            FROM message_claims c
            WHERE c.owner_session_id = session_daily_usage.session_id
              AND c.day = session_daily_usage.day
        )
        WHERE session_id IN (
            SELECT session_id FROM sessions
            WHERE source = 'claudecode' AND COALESCE(token_version, 0) >= ?
        ){scope}
        """,
        [CLAIMS_TOKEN_VERSION, *params],
    )


def refresh_native_usage(conn, session_ids=None) -> None:
    """Recompute native_* columns on sessions and session_daily_usage.

    Restricted to session_ids when given (the set whose claims or transcript
    columns changed this sync); None recomputes everything. Idempotent.
    """
    if session_ids is not None and not session_ids:
        return
    if session_ids is not None:
        chunks = [set(chunk) for chunk in _chunked(sorted(session_ids))]
    else:
        chunks = [None]
    for chunk in chunks:
        _refresh_native_mirror(conn, chunk)
        _refresh_native_claims(conn, chunk)


def get_meta(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM logpile_meta WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def set_meta(conn, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM logpile_meta WHERE key = ?", (key,))
    else:
        conn.execute(
            "INSERT OR REPLACE INTO logpile_meta (key, value) VALUES (?, ?)",
            (key, value),
        )


def insert_tool_calls(conn, session_id: str, tool_calls: list):
    conn.execute("DELETE FROM tool_calls WHERE session_id = ?", (session_id,))
    conn.executemany(
        "INSERT INTO tool_calls (session_id, tool_name, command, timestamp, is_error) VALUES (?,?,?,?,?)",
        [
            (session_id, tc.tool_name, tc.command, tc.timestamp, 1 if tc.is_error else 0)
            for tc in tool_calls
        ],
    )


def insert_session_paths(conn, session_id: str, session_paths: list):
    conn.execute("DELETE FROM session_paths WHERE session_id = ?", (session_id,))
    if not session_paths:
        return

    aggregated: dict[tuple[str, str, str, str | None], dict] = {}
    for path in session_paths:
        key = (
            path.normalized_path,
            path.operation,
            path.source,
            path.tool_name,
        )
        row = aggregated.get(key)
        if row is None:
            aggregated[key] = {
                "session_id": session_id,
                "raw_path": path.raw_path,
                "normalized_path": path.normalized_path,
                "relative_path": path.relative_path,
                "repo_relative_path": getattr(path, "repo_relative_path", None),
                "display_path": path.display_path,
                "operation": path.operation,
                "source": path.source,
                "tool_name": path.tool_name,
                "first_timestamp": path.timestamp,
                "last_timestamp": path.timestamp,
                "occurrence_count": 1,
            }
            continue

        row["occurrence_count"] += 1
        if path.timestamp:
            if not row["first_timestamp"] or path.timestamp < row["first_timestamp"]:
                row["first_timestamp"] = path.timestamp
            if not row["last_timestamp"] or path.timestamp > row["last_timestamp"]:
                row["last_timestamp"] = path.timestamp

    conn.executemany(
        """
        INSERT INTO session_paths (
            session_id,
            raw_path,
            normalized_path,
            relative_path,
            repo_relative_path,
            display_path,
            operation,
            source,
            tool_name,
            first_timestamp,
            last_timestamp,
            occurrence_count
        ) VALUES (
            :session_id,
            :raw_path,
            :normalized_path,
            :relative_path,
            :repo_relative_path,
            :display_path,
            :operation,
            :source,
            :tool_name,
            :first_timestamp,
            :last_timestamp,
            :occurrence_count
        )
        """,
        list(aggregated.values()),
    )
