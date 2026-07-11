"""SQLite database for the Logpile session index."""
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from .origins import SESSION_ORIGINS


SESSION_VISIBILITIES = ("private", "unlisted", "public")
PROFILE_VISIBILITIES = ("private", "unlisted", "public")
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
    cur = conn.execute(
        """
        UPDATE sessions
        SET visibility = ?,
            visibility_source = 'manual',
            visibility_rule_id = NULL,
            visibility_reason = 'manual override',
            is_private = CASE WHEN ? = 'private' THEN 1 ELSE 0 END
        WHERE session_id = ?
        """,
        (normalized, normalized, session_id),
    )
    if cur.rowcount:
        from .sync import reconcile_session_storage

        reconcile_session_storage(
            conn,
            shared_dir=Path(shared_dir),
            session_id=session_id,
        )
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


@contextmanager
def get_db(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
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
        "reasoning_output_tokens": 0,
        "token_version": 0,
        "parent_session_id": None,
        "spawn_depth": 0,
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
             reasoning_output_tokens, token_version, first_user_message, parent_session_id, spawn_depth, visibility,
             visibility_source, visibility_rule_id, visibility_reason,
             is_private, file_hash, synced_at, model)
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
             :reasoning_output_tokens, :token_version, :first_user_message, :parent_session_id, :spawn_depth, :visibility,
             :visibility_source, :visibility_rule_id, :visibility_reason,
             :is_private, :file_hash, :synced_at, :model)
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
            synced_at = excluded.synced_at,
            model = excluded.model
        """,
        payload,
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
