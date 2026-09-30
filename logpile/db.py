"""SQLite database for the Logpile session index."""

import os
import re
import sqlite3
import time
import warnings
from collections.abc import Iterable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from pathlib import Path

from .origins import SESSION_ORIGINS
from .parsers import ParseCheckpoint

SESSION_VISIBILITIES = ("private", "unlisted", "public")
PROFILE_VISIBILITIES = ("private", "unlisted", "public")
VISIBILITY_SOURCES = (
    "default",
    "rule",
    "manual",
    "marker",
    "review",
    "drift",
    "migration",
)

# Parses at or above this token_version emit message_claims rows, so their
# native_* columns are claims-derived. Older rows (whose bytes may be gone)
# keep native_* mirroring transcript totals — the pre-dedup approximation.
CLAIMS_TOKEN_VERSION = 5

# Search index revisions are independent from parser/token revisions. A
# mismatch schedules a streaming, per-session rebuild during the next locked
# sync; migration itself never reads transcript files.
# 3: harness-tag allowlist stripping, isMeta/tool-result-rider exclusion,
#    and codex AGENTS.md payload exclusion.
# 4: AGENTS.md payloads also dropped from structured title/goal fields.
# 5: allowlist gains heartbeat/turn_aborted/goal_context/subagent_notification,
#    wrapped-base64 removal requires MIME-shaped uniform line widths, and the
#    AGENTS.md marker requires its " for " suffix.
# 6: the AGENTS.md marker also accepts the bare-header "<INSTRUCTIONS>"
#    variant observed in the live corpus.
# 7: AGENTS.md marker requires an absolute path after "for", runs after
#    wrapper stripping, and wrapped-base64 removal requires width >= 24.
SEARCH_INDEX_VERSION = 7

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
    (
        "native_cache_creation_unknown_input_tokens",
        "cache_creation_unknown_input_tokens",
    ),
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
    cache_creation_unknown_input_tokens INTEGER DEFAULT 0,
    reasoning_output_tokens INTEGER DEFAULT 0,
    native_total_input_tokens    INTEGER DEFAULT 0,
    native_total_output_tokens   INTEGER DEFAULT 0,
    native_fresh_input_tokens    INTEGER DEFAULT 0,
    native_cached_input_tokens   INTEGER DEFAULT 0,
    native_cache_creation_input_tokens INTEGER DEFAULT 0,
    native_cache_creation_5m_input_tokens INTEGER DEFAULT 0,
    native_cache_creation_1h_input_tokens INTEGER DEFAULT 0,
    native_cache_creation_unknown_input_tokens INTEGER DEFAULT 0,
    native_reasoning_output_tokens INTEGER DEFAULT 0,
    native_assistant_message_count INTEGER DEFAULT 0,
    token_version        INTEGER DEFAULT 0,
    first_user_message    TEXT,
    thread_id             TEXT,
    parent_thread_id      TEXT,
    parent_session_id    TEXT,
    spawn_depth          INTEGER DEFAULT 0,
    identity_version     INTEGER DEFAULT 0,
    visibility     TEXT NOT NULL DEFAULT 'private',
    visibility_source TEXT NOT NULL DEFAULT 'default',
    visibility_rule_id INTEGER,
    visibility_reason TEXT,
    reviewed_sha256 TEXT,
    reviewed_artifact_path TEXT,
    publication_metadata_sha256 TEXT,
    reviewed_metadata_sha256 TEXT,
    publication_review_id INTEGER,
    publication_state TEXT NOT NULL DEFAULT 'unreviewed',
    is_private     INTEGER DEFAULT 0,
    file_hash      TEXT,
    file_size      INTEGER,
    file_mtime     REAL,
    synced_at      TEXT,
    model          TEXT
);

-- Successful publication reviews bind an operator decision to immutable,
-- hash-addressed bytes.  Rows are append-only history; sessions points at
-- the currently approved revision.
CREATE TABLE IF NOT EXISTS publication_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    reviewed_sha256 TEXT NOT NULL,
    reviewed_artifact_path TEXT NOT NULL,
    reviewed_metadata_sha256 TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    approved_visibility TEXT NOT NULL,
    forced INTEGER NOT NULL DEFAULT 0,
    successful INTEGER NOT NULL DEFAULT 1,
    reviewed_at TEXT NOT NULL
);

-- Every effective visibility change is auditable, including the deliberately
-- review-free private -> unlisted local/link transition.
CREATE TABLE IF NOT EXISTS visibility_transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    from_visibility TEXT NOT NULL,
    to_visibility TEXT NOT NULL,
    transition_source TEXT NOT NULL,
    reason TEXT,
    warning TEXT,
    publication_review_id INTEGER,
    transitioned_at TEXT NOT NULL
);

-- Copy failures survive process restarts.  Source hash/mtime are not advanced
-- until the archival copy has been hash-verified, and this row forces a retry
-- even if legacy metadata would otherwise satisfy the cheap fast path.
CREATE TABLE IF NOT EXISTS sync_copy_retries (
    source_path TEXT PRIMARY KEY,
    session_id TEXT,
    expected_sha256 TEXT,
    expected_size INTEGER,
    expected_mtime REAL,
    last_error TEXT NOT NULL,
    attempted_at TEXT NOT NULL
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
    cache_creation_unknown_input_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    native_total_input_tokens    INTEGER NOT NULL DEFAULT 0,
    native_total_output_tokens   INTEGER NOT NULL DEFAULT 0,
    native_fresh_input_tokens    INTEGER NOT NULL DEFAULT 0,
    native_cached_input_tokens   INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_cache_creation_unknown_input_tokens INTEGER NOT NULL DEFAULT 0,
    native_reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
    native_assistant_message_count INTEGER NOT NULL DEFAULT 0,
    user_message_count      INTEGER NOT NULL DEFAULT 0,
    assistant_message_count INTEGER NOT NULL DEFAULT 0,
    tool_call_count         INTEGER NOT NULL DEFAULT 0,
    approximated            INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (session_id, day)
);

-- Every Claude Code transcript occurrence is retained. Ownership is derived
-- from all current claimants, so an unchanged duplicate can be promoted when
-- the prior owner drops a claim or its session rank changes.
CREATE TABLE IF NOT EXISTS message_claims (
    claim_key TEXT NOT NULL,
    session_id TEXT NOT NULL,
    day TEXT,
    model TEXT,
    fresh_input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_creation_unknown_input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (claim_key, session_id)
) WITHOUT ROWID;

-- Small key/value state for the sync pipeline itself (e.g. the search index
-- revision, or which one-time data repairs this database has had).
CREATE TABLE IF NOT EXISTS logpile_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Sessions whose native_* columns are owed a recompute. The queue is durable,
-- so work owed by an interrupted sync survives it, and drain_native_refresh
-- can spread a large backlog across bounded syncs chunk by chunk.
CREATE TABLE IF NOT EXISTS native_refresh_queue (
    session_id TEXT PRIMARY KEY
) WITHOUT ROWID;

-- Where a resumable parse of one transcript stopped. A checkpoint names the
-- parser state file it belongs to (see parsers.ParseCheckpoint), so the next
-- sync can fold only the bytes appended since.
CREATE TABLE IF NOT EXISTS transcript_checkpoints (
    source_path TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    source TEXT NOT NULL,
    dev INTEGER NOT NULL,
    ino INTEGER NOT NULL,
    parse_offset INTEGER NOT NULL,
    parse_prefix_sha256 TEXT NOT NULL,
    parse_generation TEXT NOT NULL,
    parse_version INTEGER NOT NULL,
    state_file TEXT NOT NULL,
    file_mtime REAL,
    updated_at TEXT NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_sessions_publication_state   ON sessions(publication_state);
CREATE INDEX IF NOT EXISTS idx_sessions_status              ON sessions(session_status);
CREATE INDEX IF NOT EXISTS idx_sessions_objective_family    ON sessions(objective_family);
CREATE INDEX IF NOT EXISTS idx_sessions_origin              ON sessions(session_origin);
CREATE INDEX IF NOT EXISTS idx_sessions_parent_session      ON sessions(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_thread              ON sessions(source, username, thread_id);
CREATE INDEX IF NOT EXISTS idx_sessions_parent_thread       ON sessions(source, username, parent_thread_id);
CREATE INDEX IF NOT EXISTS idx_session_daily_day            ON session_daily_usage(day);
CREATE INDEX IF NOT EXISTS idx_message_claims_session_day   ON message_claims(session_id, day);
CREATE INDEX IF NOT EXISTS idx_tool_calls_session           ON tool_calls(session_id);
CREATE INDEX IF NOT EXISTS idx_tool_calls_name              ON tool_calls(tool_name);
CREATE INDEX IF NOT EXISTS idx_session_paths_session        ON session_paths(session_id);
CREATE INDEX IF NOT EXISTS idx_session_paths_display        ON session_paths(display_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_relative       ON session_paths(relative_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_repo_relative  ON session_paths(repo_relative_path);
CREATE INDEX IF NOT EXISTS idx_session_paths_normalized     ON session_paths(normalized_path);
CREATE INDEX IF NOT EXISTS idx_rules_username               ON session_visibility_rules(username);
CREATE INDEX IF NOT EXISTS idx_rules_priority               ON session_visibility_rules(username, enabled, priority, id);
CREATE INDEX IF NOT EXISTS idx_publication_reviews_session  ON publication_reviews(session_id, reviewed_at DESC);
CREATE INDEX IF NOT EXISTS idx_visibility_transitions_session ON visibility_transitions(session_id, transitioned_at DESC);
CREATE INDEX IF NOT EXISTS idx_transcript_checkpoints_session ON transcript_checkpoints(session_id);
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
    d.cache_creation_unknown_input_tokens,
    d.reasoning_output_tokens,
    d.native_total_input_tokens,
    d.native_total_output_tokens,
    d.native_fresh_input_tokens,
    d.native_cached_input_tokens,
    d.native_cache_creation_input_tokens,
    d.native_cache_creation_5m_input_tokens,
    d.native_cache_creation_1h_input_tokens,
    d.native_cache_creation_unknown_input_tokens,
    d.native_reasoning_output_tokens,
    d.native_assistant_message_count,
    d.user_message_count,
    d.assistant_message_count,
    d.tool_call_count,
    d.approximated
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
    COALESCE(s.cache_creation_unknown_input_tokens, 0),
    COALESCE(s.reasoning_output_tokens, 0),
    COALESCE(s.native_total_input_tokens, 0),
    COALESCE(s.native_total_output_tokens, 0),
    COALESCE(s.native_fresh_input_tokens, 0),
    COALESCE(s.native_cached_input_tokens, 0),
    COALESCE(s.native_cache_creation_input_tokens, 0),
    COALESCE(s.native_cache_creation_5m_input_tokens, 0),
    COALESCE(s.native_cache_creation_1h_input_tokens, 0),
    COALESCE(s.native_cache_creation_unknown_input_tokens, 0),
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

-- The minimum-ranked live claimant owns each message. NULL timestamps rank
-- after dated sessions, and session_id is the deterministic final tie-break.
DROP VIEW IF EXISTS message_claim_owners;
CREATE VIEW message_claim_owners AS
SELECT c.claim_key, c.session_id AS owner_session_id
FROM message_claims c
JOIN sessions s ON s.session_id = c.session_id
WHERE NOT EXISTS (
    SELECT 1
    FROM message_claims c2
    JOIN sessions s2 ON s2.session_id = c2.session_id
    WHERE c2.claim_key = c.claim_key
      AND (
          COALESCE(s2.last_timestamp, '~') < COALESCE(s.last_timestamp, '~')
          OR (
              COALESCE(s2.last_timestamp, '~') = COALESCE(s.last_timestamp, '~')
              AND COALESCE(s2.first_timestamp, '~') < COALESCE(s.first_timestamp, '~')
          )
          OR (
              COALESCE(s2.last_timestamp, '~') = COALESCE(s.last_timestamp, '~')
              AND COALESCE(s2.first_timestamp, '~') = COALESCE(s.first_timestamp, '~')
              AND c2.session_id < c.session_id
          )
      )
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


SEARCH_SCHEMA = """
-- Searchable documents retain one bounded text block per row. The B-tree
-- table gives per-session replacement an indexed delete path; the external-
-- content FTS table stores only its term index and reads snippets from these
-- rows instead of duplicating transcript text a second time.
CREATE TABLE IF NOT EXISTS session_search_documents (
    id              INTEGER PRIMARY KEY,
    session_id      TEXT NOT NULL,
    field_label     TEXT NOT NULL,
    chunk_index     INTEGER NOT NULL,
    structured_text TEXT,
    transcript_text TEXT,
    UNIQUE (session_id, field_label, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_session_search_documents_session
    ON session_search_documents(session_id);

CREATE TABLE IF NOT EXISTS session_search_state (
    session_id        TEXT PRIMARY KEY,
    search_version    INTEGER NOT NULL,
    file_hash         TEXT,
    artifact_hash     TEXT,
    metadata_hash     TEXT NOT NULL,
    transcript_status TEXT NOT NULL,
    last_error        TEXT,
    indexed_at        TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS session_search_fts USING fts5(
    structured_text,
    transcript_text,
    content='session_search_documents',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS session_search_documents_ai
AFTER INSERT ON session_search_documents BEGIN
    INSERT INTO session_search_fts(rowid, structured_text, transcript_text)
    VALUES (new.id, new.structured_text, new.transcript_text);
END;

CREATE TRIGGER IF NOT EXISTS session_search_documents_ad
AFTER DELETE ON session_search_documents BEGIN
    INSERT INTO session_search_fts(
        session_search_fts, rowid, structured_text, transcript_text
    ) VALUES (
        'delete', old.id, old.structured_text, old.transcript_text
    );
END;

CREATE TRIGGER IF NOT EXISTS session_search_documents_au
AFTER UPDATE ON session_search_documents BEGIN
    INSERT INTO session_search_fts(
        session_search_fts, rowid, structured_text, transcript_text
    ) VALUES (
        'delete', old.id, old.structured_text, old.transcript_text
    );
    INSERT INTO session_search_fts(rowid, structured_text, transcript_text)
    VALUES (new.id, new.structured_text, new.transcript_text);
END;

CREATE TRIGGER IF NOT EXISTS sessions_search_cleanup
AFTER DELETE ON sessions BEGIN
    DELETE FROM session_search_documents WHERE session_id = old.session_id;
    DELETE FROM session_search_state WHERE session_id = old.session_id;
END;

-- Any direct mutation of indexed inputs invalidates the prior revision before
-- it can be returned. Normal sync upserts write many columns even when their
-- values are unchanged, so the WHEN clause deliberately uses null-safe value
-- comparisons to avoid needless rebuilds.
CREATE TRIGGER IF NOT EXISTS sessions_search_stale
AFTER UPDATE OF
    source, file_hash, session_goal, session_summary, first_user_message,
    repo_name, project, visibility, reviewed_sha256, reviewed_artifact_path,
    publication_metadata_sha256, reviewed_metadata_sha256
ON sessions
WHEN
       NOT (old.source IS new.source)
    OR NOT (old.file_hash IS new.file_hash)
    OR NOT (old.session_goal IS new.session_goal)
    OR NOT (old.session_summary IS new.session_summary)
    OR NOT (old.first_user_message IS new.first_user_message)
    OR NOT (old.repo_name IS new.repo_name)
    OR NOT (old.project IS new.project)
    OR NOT (old.visibility IS new.visibility)
    OR (
        (old.visibility = 'public' OR new.visibility = 'public')
        AND (
               NOT (old.reviewed_sha256 IS new.reviewed_sha256)
            OR NOT (old.reviewed_artifact_path IS new.reviewed_artifact_path)
            OR NOT (old.publication_metadata_sha256 IS new.publication_metadata_sha256)
            OR NOT (old.reviewed_metadata_sha256 IS new.reviewed_metadata_sha256)
        )
    )
BEGIN
    UPDATE session_search_state
    SET transcript_status = 'stale', artifact_hash = NULL
    WHERE session_id = new.session_id;
END;
"""

# Rows derived from a session leave with it. Deleting a session's claims can
# promote another claimant of the same message to owner, so every other
# claimant of those keys is queued for a native refresh. The CROSS JOIN pins
# the join order: this session's keys come from its (session_id, day) index,
# and each key's claimants from the (claim_key, session_id) primary key.
SESSION_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS sessions_message_claims_cleanup
AFTER DELETE ON sessions BEGIN
    INSERT OR IGNORE INTO native_refresh_queue (session_id)
    SELECT DISTINCT other.session_id
    FROM message_claims AS mine
    CROSS JOIN message_claims AS other ON other.claim_key = mine.claim_key
    WHERE mine.session_id = old.session_id
      AND other.session_id != old.session_id;
    DELETE FROM message_claims WHERE session_id = old.session_id;
    DELETE FROM native_refresh_queue WHERE session_id = old.session_id;
END;

CREATE TRIGGER IF NOT EXISTS sessions_transcript_checkpoints_cleanup
AFTER DELETE ON sessions BEGIN
    DELETE FROM transcript_checkpoints WHERE session_id = old.session_id;
END;
"""

# Views and triggers whose definitions migrate_db replaces when they drift.
# Each is compared against its canonical sqlite_master text and rewritten only
# when that text differs: a DROP + CREATE writes schema pages to the WAL and
# bumps PRAGMA schema_version even when the definition is identical.
_MANAGED_TRIGGERS = (
    "sessions_search_stale",
    "sessions_message_claims_cleanup",
    "sessions_transcript_checkpoints_cleanup",
)

# One-time repairs of rows written by older Logpile versions. Current writers
# never produce the states these heal, so they run once per database (and
# again whenever a migration changes the schema), not on every sync. Bump the
# version when a new repair is added so it reaches databases that already
# recorded the old one.
DATA_REPAIR_VERSION = 1
_DATA_REPAIR_META_KEY = "data_repair_version"

# Superseded by native_refresh_queue. drain_native_refresh adopts a pending
# value left by an older sync (see _adopt_legacy_native_refresh_flag).
_LEGACY_NATIVE_REFRESH_FLAG = "native_refresh_pending"

_canonical_schema_sql: dict[str, tuple[str, str]] | None = None


def _canonical_managed_schema() -> dict[str, tuple[str, str]]:
    """Return {name: (type, sql)} as sqlite_master stores each managed object.

    SQLite normalizes the stored CREATE text (it drops IF NOT EXISTS, for
    example), so the reference text comes from creating the objects in a
    scratch in-memory database rather than from the source strings.
    """
    global _canonical_schema_sql
    if _canonical_schema_sql is None:
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(SCHEMA)
            reference.executescript(SEARCH_SCHEMA)
            reference.executescript(VIEWS)
            reference.executescript(SESSION_TRIGGERS)
            placeholders = ", ".join("?" for _ in _MANAGED_TRIGGERS)
            _canonical_schema_sql = {
                name: (kind, sql)
                for kind, name, sql in reference.execute(
                    "SELECT type, name, sql FROM sqlite_master "
                    f"WHERE type = 'view' OR (type = 'trigger' AND name IN ({placeholders}))",
                    _MANAGED_TRIGGERS,
                )
            }
        finally:
            reference.close()
    return _canonical_schema_sql


def _replace_drifted_schema_objects(conn: sqlite3.Connection, kind: str) -> None:
    """Recreate managed views or triggers whose stored SQL is not canonical."""
    stored = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = ?", (kind,)
        )
    }
    for name, (object_kind, sql) in _canonical_managed_schema().items():
        if object_kind != kind or stored.get(name) == sql:
            continue
        conn.execute(f"DROP {kind.upper()} IF EXISTS {_quote_identifier(name)}")
        conn.execute(sql)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalize_username(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", (value or "").strip().lower()).strip("-")
    return normalized or "user"


def _normalize_visibility(
    value: str | None,
    allowed: tuple[str, ...],
) -> str:
    """Normalize known visibility values and reject everything else closed."""
    if value is None:
        raise ValueError("Visibility is required")
    normalized = str(value).strip().lower()
    if normalized not in allowed:
        raise ValueError(f"Unsupported visibility: {value}")
    return normalized


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


def _ensure_column(
    conn: sqlite3.Connection, table_name: str, column_name: str, spec: str
) -> None:
    if column_name in _table_columns(conn, table_name):
        return
    conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {spec}")


def _next_available_username(conn: sqlite3.Connection, base_username: str) -> str:
    username = base_username
    suffix = 2
    while conn.execute(
        "SELECT 1 FROM users WHERE username = ?", (username,)
    ).fetchone():
        username = f"{base_username}-{suffix}"
        suffix += 1
    return username


def _publication_session_row(conn: sqlite3.Connection, session_id: str):
    """Load a session with every mutable, publicly rendered review field."""
    return conn.execute(
        """
        SELECT s.*, COALESCE(u.display_name, u.username, s.username) AS display_name,
               u.bio AS bio, u.avatar_url AS avatar_url
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.session_id = ?
        """,
        (session_id,),
    ).fetchone()


def refresh_session_publication_metadata(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    reason: str = "rendered metadata drifted from the reviewed revision",
) -> bool:
    """Refresh one review fingerprint and revoke a stale public rendering.

    Parser backfills and user-profile edits can change public metadata without
    going through the main session upsert.  They call this same fail-closed
    hook so Next and Flask never pair reviewed bytes with unreviewed labels.
    Returns whether a public session was revoked.
    """
    from .publish import publication_metadata_sha256

    row = _publication_session_row(conn, session_id)
    if row is None:
        return False
    current_metadata_sha256 = publication_metadata_sha256(row)
    conn.execute(
        "UPDATE sessions SET publication_metadata_sha256 = ? WHERE session_id = ?",
        (current_metadata_sha256, session_id),
    )
    if (
        row["visibility"] != "public"
        or row["reviewed_metadata_sha256"] == current_metadata_sha256
    ):
        return False

    full_reason = f"{reason}; publication revoked and requeued"
    transition_session_visibility(
        conn,
        session_id,
        "unlisted",
        shared_dir=None,
        transition_source="drift",
        reason=full_reason,
        manage_storage=False,
    )
    conn.execute(
        """
        UPDATE sessions
        SET publication_state = 'source_drift',
            visibility_reason = ?
        WHERE session_id = ?
        """,
        (full_reason, session_id),
    )
    return True


def _refresh_user_publication_metadata(
    conn: sqlite3.Connection,
    username: str,
) -> None:
    """Re-fingerprint display names and revoke any now-stale publication."""
    session_ids = conn.execute(
        "SELECT session_id FROM sessions WHERE username = ?",
        (username,),
    ).fetchall()
    for row in session_ids:
        refresh_session_publication_metadata(
            conn,
            row["session_id"],
            reason="rendered user metadata drifted from the reviewed revision",
        )


def ensure_user(
    conn: sqlite3.Connection, username: str, display_name: str | None = None
) -> str:
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
            _refresh_user_publication_metadata(conn, row["username"])
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
            profile_visibility, PROFILE_VISIBILITIES
        )
    if default_session_visibility is not None:
        updates["default_session_visibility"] = _normalize_visibility(
            default_session_visibility,
            SESSION_VISIBILITIES,
        )

    assignments = ", ".join(f"{column} = :{column}" for column in updates)
    updates["username"] = user["username"]
    conn.execute(f"UPDATE users SET {assignments} WHERE username = :username", updates)
    if any(
        value is not None and value != user[field]
        for field, value in (
            ("display_name", display_name),
            ("bio", bio),
            ("avatar_url", avatar_url),
        )
    ):
        _refresh_user_publication_metadata(conn, user["username"])
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


@dataclass(frozen=True)
class VisibilityTransitionResult:
    count: int
    session_id: str | None
    from_visibility: str | None
    to_visibility: str | None
    warning: str | None = None
    publication_review_id: int | None = None


def _successful_publication_review(
    conn: sqlite3.Connection,
    session_id: str,
    review_id: int | None,
):
    if review_id is None:
        row = conn.execute(
            "SELECT publication_review_id FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        review_id = row["publication_review_id"] if row else None
    if review_id is None:
        return None
    return conn.execute(
        """
        SELECT r.*
        FROM publication_reviews r
        JOIN sessions s ON s.session_id = r.session_id
        WHERE r.id = ?
          AND r.session_id = ?
          AND r.successful = 1
          AND r.approved_visibility = 'public'
          AND r.reviewed_sha256 = s.reviewed_sha256
          AND s.file_hash IS NOT NULL
          AND substr(r.reviewed_sha256, 1, length(s.file_hash)) = s.file_hash
          AND r.reviewed_artifact_path = s.reviewed_artifact_path
          AND r.reviewed_metadata_sha256 = s.reviewed_metadata_sha256
          AND s.reviewed_metadata_sha256 = s.publication_metadata_sha256
        """,
        (review_id, session_id),
    ).fetchone()


def transition_session_visibility(
    conn: sqlite3.Connection,
    session_id_prefix: str,
    visibility: str,
    *,
    shared_dir: Path | None,
    transition_source: str = "manual",
    reason: str | None = None,
    visibility_rule_id: int | None = None,
    publication_review_id: int | None = None,
    public_without_review: str = "raise",
    manage_storage: bool = True,
    storage_transition=None,
) -> VisibilityTransitionResult:
    """Apply one guarded, audited session-visibility transition.

    All manual, rule/default, marker, migration, and review transitions route
    through this function.  ``public`` is fail-closed unless a successful
    review is bound to the current source hash.  Automated defaults/rules may
    ask to fall back to local-only ``unlisted`` instead of aborting sync.
    """
    normalized = _normalize_visibility(visibility, SESSION_VISIBILITIES)
    normalized_source = (transition_source or "").strip().lower()
    if normalized_source not in VISIBILITY_SOURCES:
        raise ValueError(
            f"Unsupported visibility transition source: {transition_source}"
        )
    if public_without_review not in {"raise", "unlisted"}:
        raise ValueError(f"Unsupported public review fallback: {public_without_review}")

    session_id = resolve_session_id(conn, session_id_prefix)
    if not session_id:
        return VisibilityTransitionResult(0, None, None, None)
    row = _publication_session_row(conn, session_id)
    from .publish import publication_metadata_sha256

    current_metadata_sha256 = publication_metadata_sha256(row)
    if row["publication_metadata_sha256"] != current_metadata_sha256:
        conn.execute(
            "UPDATE sessions SET publication_metadata_sha256 = ? WHERE session_id = ?",
            (current_metadata_sha256, session_id),
        )
        row = _publication_session_row(conn, session_id)
    raw_current = row["visibility"]
    invalid_current = False
    try:
        current = _normalize_visibility(raw_current, SESSION_VISIBILITIES)
    except ValueError:
        # Only the guarded transition API may repair legacy/corrupt stored
        # values. Treat them as private, record the original value below, and
        # never let a typo become publishable.
        current = "private"
        invalid_current = True
    warning: str | None = None
    review = None
    if normalized == "public":
        review = _successful_publication_review(conn, session_id, publication_review_id)
        if review is None:
            if public_without_review == "raise":
                raise ValueError(
                    "Public visibility requires a successful review of the current revision. "
                    "Run `logpile publish approve` first."
                )
            normalized = "unlisted"
            warning = (
                "Public visibility requires a successful review record; kept this "
                "session unlisted for local/link access."
            )
    if (
        current == "private"
        and normalized == "unlisted"
        and warning is None
        and publication_review_id is None
    ):
        warning = (
            "Unlisted sessions are local/link artifacts and are not served in "
            "public mode; no publish review was required."
        )

    transition = storage_transition
    if manage_storage and transition is None and normalized != current:
        if shared_dir is None:
            raise ValueError(
                "shared_dir is required for a stored visibility transition"
            )
        if normalized == "private":
            from .sync import prepare_private_session_storage

            transition = prepare_private_session_storage(
                row, shared_dir=Path(shared_dir)
            )
        elif current == "private":
            from .sync import prepare_shared_session_storage

            transition = prepare_shared_session_storage(
                row, shared_dir=Path(shared_dir)
            )

    next_shared_path = (
        str(transition.archive_path) if transition is not None else row["shared_path"]
    )
    effective_reason = reason or f"{normalized_source} visibility transition"
    effective_rule_id = visibility_rule_id if normalized_source == "rule" else None
    effective_review_id = review["id"] if review is not None else publication_review_id
    stored_source = "manual" if normalized_source == "review" else normalized_source
    try:
        cur = conn.execute(
            """
            UPDATE sessions
            SET visibility = ?,
                visibility_source = ?,
                visibility_rule_id = ?,
                visibility_reason = ?,
                is_private = CASE WHEN ? = 'private' THEN 1 ELSE 0 END,
                shared_path = ?,
                publication_review_id = CASE
                    WHEN ? = 'public' THEN ?
                    ELSE publication_review_id
                END,
                publication_state = CASE
                    WHEN ? = 'public' THEN 'reviewed'
                    WHEN ? = 'private' THEN 'revoked'
                    WHEN reviewed_sha256 IS NOT NULL
                     AND file_hash IS NOT NULL
                     AND substr(reviewed_sha256, 1, length(file_hash)) = file_hash
                     AND reviewed_metadata_sha256 IS NOT NULL
                     AND reviewed_metadata_sha256 = publication_metadata_sha256
                    THEN 'reviewed'
                    ELSE 'unreviewed'
                END
            WHERE session_id = ?
            """,
            (
                normalized,
                stored_source,
                effective_rule_id,
                effective_reason,
                normalized,
                next_shared_path,
                normalized,
                effective_review_id,
                normalized,
                normalized,
                session_id,
            ),
        )
        if current != normalized or invalid_current:
            conn.execute(
                """
                INSERT INTO visibility_transitions (
                    session_id, from_visibility, to_visibility,
                    transition_source, reason, warning,
                    publication_review_id, transitioned_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    str(raw_current) if invalid_current else current,
                    normalized,
                    normalized_source,
                    effective_reason,
                    warning,
                    effective_review_id,
                    _now_iso(),
                ),
            )
    except BaseException:
        if transition is not None:
            transition.rollback()
        raise
    if transition is not None:
        defer_storage_transition(conn, transition)
    return VisibilityTransitionResult(
        cur.rowcount,
        session_id,
        current,
        normalized,
        warning,
        effective_review_id,
    )


def set_session_visibility(
    conn: sqlite3.Connection,
    session_id_prefix: str,
    visibility: str,
    *,
    shared_dir: Path,
    publication_review_id: int | None = None,
) -> int:
    """Compatibility wrapper for manual callers around the guarded API."""
    result = transition_session_visibility(
        conn,
        session_id_prefix,
        visibility,
        shared_dir=shared_dir,
        transition_source="review" if publication_review_id is not None else "manual",
        reason="reviewed publish approval"
        if publication_review_id is not None
        else "manual override",
        publication_review_id=publication_review_id,
    )
    if result.warning:
        warnings.warn(result.warning, RuntimeWarning, stacklevel=2)
    return result.count


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
    normalized_visibility = _normalize_visibility(visibility, SESSION_VISIBILITIES)
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

    normalized_default = _normalize_visibility(default_visibility, SESSION_VISIBILITIES)
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
        result = transition_session_visibility(
            conn,
            row["session_id"],
            str(decision["visibility"]),
            shared_dir=Path(shared_dir),
            transition_source=str(decision["visibility_source"]),
            reason=str(decision["visibility_reason"]),
            visibility_rule_id=decision["visibility_rule_id"],
            public_without_review="unlisted",
        )
        if result.warning:
            warnings.warn(result.warning, RuntimeWarning, stacklevel=2)
        updated += result.count

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


def _quote_identifier(value: str) -> str:
    """Quote a SQLite identifier discovered from the database schema."""
    return '"' + value.replace('"', '""') + '"'


def _snapshot_before_identity_rebuild(conn: sqlite3.Connection) -> Path | None:
    """Create a durable snapshot immediately before destructive table rebuilds.

    In-memory databases used by unit tests have no file to preserve.  On-disk
    databases get a sibling, mode-0600 SQLite backup; an existing snapshot is
    never overwritten, so a failed migration can be retried without destroying
    the first known-good copy.
    """
    database_path = ""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            database_path = row[2] or ""
            break
    if not database_path:
        return None

    source_path = Path(database_path)
    base = source_path.with_name(f"{source_path.name}.pre-identity-migration.sqlite")
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = base.with_name(f"{base.name}.{suffix}")
        suffix += 1

    # The online-backup API needs a stable transaction boundary and includes
    # committed WAL pages in the destination database.
    conn.commit()
    fd = os.open(candidate, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        with sqlite3.connect(candidate) as snapshot:
            conn.backup(snapshot)
            # A backup copies the source database's WAL-mode header.  A cold
            # migration snapshot is one self-contained file, not a main file
            # plus empty sidecars that callers might forget to restore.
            snapshot.execute("PRAGMA journal_mode=DELETE").fetchone()
            check = snapshot.execute("PRAGMA quick_check").fetchone()
            if not check or check[0] != "ok":
                raise sqlite3.DatabaseError(
                    f"migration snapshot failed quick_check: {check[0] if check else 'no result'}"
                )
    except BaseException:
        candidate.unlink(missing_ok=True)
        raise
    candidate.chmod(0o600)
    return candidate


def _rebuild_column_plan(
    conn: sqlite3.Connection,
    table_name: str,
    *,
    excluded: set[str] | None = None,
    renamed: dict[str, str] | None = None,
    primary_key: str,
    autoincrement: bool = False,
) -> tuple[list[sqlite3.Row], list[tuple[str, str]], str]:
    """Return source metadata, source/target names, and dynamic CREATE SQL."""
    excluded = excluded or set()
    renamed = renamed or {}
    info = conn.execute(
        f"PRAGMA table_info({_quote_identifier(table_name)})"
    ).fetchall()
    pairs: list[tuple[str, str]] = []
    declarations: list[str] = []
    seen: set[str] = set()
    for column in info:
        source_name = column[1]
        if source_name in excluded:
            continue
        target_name = renamed.get(source_name, source_name)
        if target_name in seen:
            continue
        seen.add(target_name)
        pairs.append((source_name, target_name))

        column_type = (column[2] or "").strip()
        declaration = _quote_identifier(target_name)
        if column_type:
            declaration += f" {column_type}"
        if target_name == primary_key:
            declaration += " PRIMARY KEY"
            if autoincrement:
                declaration += " AUTOINCREMENT"
        elif column[3]:
            declaration += " NOT NULL"
        if column[4] is not None:
            declaration += f" DEFAULT {column[4]}"
        declarations.append(declaration)

    if primary_key not in seen:
        raise sqlite3.DatabaseError(
            f"cannot rebuild {table_name}: missing primary key column {primary_key}"
        )
    create_sql = ",\n                ".join(declarations)
    return info, pairs, create_sql


def _insert_dynamic_row(
    conn: sqlite3.Connection,
    table_name: str,
    column_names: list[str],
    values: list[object],
) -> None:
    columns_sql = ", ".join(_quote_identifier(name) for name in column_names)
    placeholders = ", ".join("?" for _ in column_names)
    conn.execute(
        f"INSERT INTO {_quote_identifier(table_name)} ({columns_sql}) "
        f"VALUES ({placeholders})",
        values,
    )


def _next_migration_username(base: str, used: set[str]) -> str:
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


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
    _snapshot_before_identity_rebuild(conn)

    user_rows = [dict(row) for row in conn.execute("SELECT * FROM users").fetchall()]
    session_rows = [
        dict(row) for row in conn.execute("SELECT * FROM sessions").fetchall()
    ]
    rule_rows = [
        dict(row)
        for row in conn.execute("SELECT * FROM session_visibility_rules").fetchall()
    ]
    github_columns = _table_columns(conn, "user_github_daily")
    github_usernames = (
        [
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT username FROM user_github_daily "
                "WHERE username IS NOT NULL AND username != '' ORDER BY username"
            ).fetchall()
        ]
        if "username" in github_columns
        else []
    )

    def user_identity(row: dict) -> str:
        return str(row.get("username") or row.get("slug") or "user")

    # Binary spelling is the final tie-breaker so case-only collisions resolve
    # identically regardless of insertion order or SQLite rowid allocation.
    user_rows.sort(
        key=lambda row: (
            normalize_username(user_identity(row)),
            user_identity(row).casefold(),
            user_identity(row),
            str(row.get("slug") or ""),
        )
    )
    used_usernames: set[str] = set()
    username_map: dict[str, str] = {}
    slug_map: dict[str, str] = {}
    assigned_rows: list[tuple[dict, str]] = []
    for row in user_rows:
        canonical = _next_migration_username(
            normalize_username(user_identity(row)), used_usernames
        )
        assigned_rows.append((row, canonical))
        if row.get("username") not in (None, ""):
            username_map.setdefault(str(row["username"]), canonical)
        if row.get("slug") not in (None, ""):
            slug_map.setdefault(str(row["slug"]), canonical)

    def mapped_identity(row: dict, *, legacy_slug: bool) -> str | None:
        if legacy_slug and row.get("user_slug") not in (None, ""):
            mapped = slug_map.get(str(row["user_slug"]))
            if mapped:
                return mapped
        if row.get("username") not in (None, ""):
            mapped = username_map.get(str(row["username"]))
            if mapped:
                return mapped
        return None

    orphan_values: set[str] = set()
    for row in session_rows:
        if mapped_identity(row, legacy_slug="user_slug" in session_columns) is None:
            orphan_values.add(
                str(row.get("user_slug") or row.get("username") or "user")
            )
    for row in rule_rows:
        if mapped_identity(row, legacy_slug="user_slug" in rule_columns) is None:
            orphan_values.add(
                str(row.get("user_slug") or row.get("username") or "user")
            )
    for raw_username in github_usernames:
        if raw_username not in username_map and raw_username not in slug_map:
            orphan_values.add(raw_username)
    orphan_map: dict[str, str] = {}
    for raw_value in sorted(
        orphan_values,
        key=lambda value: (normalize_username(value), value.casefold(), value),
    ):
        orphan_map[raw_value] = _next_migration_username(
            normalize_username(raw_value), used_usernames
        )

    def resolve_identity(row: dict, *, legacy_slug: bool) -> str:
        mapped = mapped_identity(row, legacy_slug=legacy_slug)
        if mapped:
            return mapped
        raw_value = str(row.get("user_slug") or row.get("username") or "user")
        return orphan_map[raw_value]

    _user_info, user_pairs, user_create = _rebuild_column_plan(
        conn,
        "users",
        excluded={"slug"},
        primary_key="username",
    )
    rule_renamed = {"user_slug": "username"} if "username" not in rule_columns else {}
    rule_excluded = {"user_slug"} if "username" in rule_columns else set()
    _rule_info, rule_pairs, rule_create = _rebuild_column_plan(
        conn,
        "session_visibility_rules",
        excluded=rule_excluded,
        renamed=rule_renamed,
        primary_key="id",
        autoincrement=True,
    )
    _session_info, session_pairs, session_create = _rebuild_column_plan(
        conn,
        "sessions",
        excluded={"user_slug"},
        primary_key="session_id",
    )

    conn.execute("SAVEPOINT identity_schema_rebuild")
    try:
        conn.execute("DROP VIEW IF EXISTS session_catalog")
        conn.execute("DROP VIEW IF EXISTS user_catalog")
        for table_name in (
            "users__new",
            "session_visibility_rules__new",
            "sessions__new",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {_quote_identifier(table_name)}")
        conn.execute(f"CREATE TABLE users__new ({user_create})")
        conn.execute(f"CREATE TABLE session_visibility_rules__new ({rule_create})")
        conn.execute(f"CREATE TABLE sessions__new ({session_create})")

        user_target_names = [target for _source, target in user_pairs]
        for row, canonical in assigned_rows:
            values = [
                canonical if target == "username" else row.get(source)
                for source, target in user_pairs
            ]
            _insert_dynamic_row(conn, "users__new", user_target_names, values)

        # References without a legacy users row still need a canonical owner.
        # Insert only known profile columns so any additional legacy columns
        # retain their declared defaults instead of being overwritten.
        available_user_columns = set(user_target_names)
        for raw_value, canonical in sorted(
            orphan_map.items(), key=lambda item: item[1]
        ):
            defaults = {
                "username": canonical,
                "display_name": raw_value,
                "profile_visibility": "public",
                "default_session_visibility": "unlisted",
                "created_at": now,
                "updated_at": now,
            }
            names = [name for name in defaults if name in available_user_columns]
            _insert_dynamic_row(
                conn,
                "users__new",
                names,
                [defaults[name] for name in names],
            )

        rule_target_names = [target for _source, target in rule_pairs]
        for row in rule_rows:
            canonical = resolve_identity(row, legacy_slug="user_slug" in rule_columns)
            values = [
                canonical if target == "username" else row.get(source)
                for source, target in rule_pairs
            ]
            _insert_dynamic_row(
                conn, "session_visibility_rules__new", rule_target_names, values
            )

        session_target_names = [target for _source, target in session_pairs]
        for row in session_rows:
            canonical = resolve_identity(
                row, legacy_slug="user_slug" in session_columns
            )
            values = [
                canonical if target == "username" else row.get(source)
                for source, target in session_pairs
            ]
            _insert_dynamic_row(conn, "sessions__new", session_target_names, values)

        # This table is not rebuilt, so every provider/count/custom column is
        # preserved in place. Use collision-safe temporary owners before the
        # final canonical names (for example `Alice` -> `alice` while the old
        # `alice` row must first move to `alice-2`).
        github_remaps: list[tuple[str, str]] = []
        for raw_username in github_usernames:
            canonical = (
                username_map.get(raw_username)
                or slug_map.get(raw_username)
                or orphan_map[raw_username]
            )
            if canonical != raw_username:
                github_remaps.append((raw_username, canonical))
        occupied_github_names = set(github_usernames) | {
            canonical for _raw, canonical in github_remaps
        }
        staged_github_names: list[tuple[str, str]] = []
        for index, (raw_username, canonical) in enumerate(github_remaps):
            temporary = f"__logpile_identity_migration_{index}__"
            while temporary in occupied_github_names:
                temporary += "_"
            occupied_github_names.add(temporary)
            conn.execute(
                "UPDATE user_github_daily SET username = ? WHERE username = ?",
                (temporary, raw_username),
            )
            staged_github_names.append((temporary, canonical))
        for temporary, canonical in staged_github_names:
            conn.execute(
                "UPDATE user_github_daily SET username = ? WHERE username = ?",
                (canonical, temporary),
            )

        conn.execute("DROP TABLE session_visibility_rules")
        conn.execute(
            "ALTER TABLE session_visibility_rules__new RENAME TO session_visibility_rules"
        )
        conn.execute("DROP TABLE sessions")
        conn.execute("ALTER TABLE sessions__new RENAME TO sessions")
        conn.execute("DROP TABLE users")
        conn.execute("ALTER TABLE users__new RENAME TO users")
        conn.execute("RELEASE SAVEPOINT identity_schema_rebuild")
    except BaseException:
        conn.execute("ROLLBACK TO SAVEPOINT identity_schema_rebuild")
        conn.execute("RELEASE SAVEPOINT identity_schema_rebuild")
        raise


def _migrate_message_claim_occurrences(conn: sqlite3.Connection) -> None:
    """Upgrade the winner-only claim ledger to one row per occurrence.

    The legacy winner is retained as an occurrence. A token-version resync
    subsequently repopulates unchanged losing claimants from available source
    or shared transcripts. Queueing every session for a native refresh
    preserves the interrupted-sync recovery contract across this schema
    transition.
    """
    columns = _table_columns(conn, "message_claims")
    if "owner_session_id" in columns and "session_id" not in columns:
        conn.execute("DROP VIEW IF EXISTS message_claim_owners")
        conn.execute("DROP INDEX IF EXISTS idx_message_claims_owner_day")
        # The delete trigger names message_claims.session_id, which the
        # legacy table lacks, and SQLite rejects the RENAME below while any
        # trigger body fails to resolve. migrate_db recreates it at the end.
        conn.execute("DROP TRIGGER IF EXISTS sessions_message_claims_cleanup")
        conn.execute("ALTER TABLE message_claims RENAME TO message_claims__winners")
        conn.execute(
            """
            CREATE TABLE message_claims (
                claim_key TEXT NOT NULL,
                session_id TEXT NOT NULL,
                day TEXT,
                model TEXT,
                fresh_input_tokens INTEGER NOT NULL DEFAULT 0,
                cached_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_5m_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_1h_input_tokens INTEGER NOT NULL DEFAULT 0,
                cache_creation_unknown_input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (claim_key, session_id)
            ) WITHOUT ROWID
            """
        )
        conn.execute(
            """
            INSERT INTO message_claims (
                claim_key, session_id, day, model,
                fresh_input_tokens, cached_input_tokens,
                cache_creation_input_tokens,
                cache_creation_5m_input_tokens,
                cache_creation_1h_input_tokens,
                cache_creation_unknown_input_tokens,
                output_tokens
            )
            SELECT
                claim_key, owner_session_id, day, model,
                fresh_input_tokens, cached_input_tokens,
                cache_creation_input_tokens,
                CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_5m_input_tokens ELSE 0
                END,
                CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_1h_input_tokens ELSE 0
                END,
                CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_input_tokens
                         - cache_creation_5m_input_tokens
                         - cache_creation_1h_input_tokens
                    ELSE cache_creation_input_tokens
                END,
                output_tokens
            FROM message_claims__winners
            """
        )
        conn.execute("DROP TABLE message_claims__winners")
        conn.execute(
            "INSERT OR IGNORE INTO native_refresh_queue (session_id) "
            "SELECT session_id FROM sessions"
        )
    else:
        _ensure_column(
            conn,
            "message_claims",
            "cache_creation_unknown_input_tokens",
            "INTEGER NOT NULL DEFAULT 0",
        )


def _drop_orphan_message_claims(conn: sqlite3.Connection) -> None:
    """Remove claims whose session row is gone and queue what that can move.

    message_claim_owners joins sessions, so an orphan never owned a message
    and deleting it changes no owner. The live claimants of the same keys are
    queued anyway: they are the sessions whose native_* went stale when the
    orphan's session was deleted without a refresh. The sessions delete
    trigger now does this at delete time, so only rows written before that
    trigger existed (or by hand) can still be orphaned.
    """
    orphan_ids = [
        row[0]
        for row in conn.execute(
            "SELECT DISTINCT session_id FROM message_claims "
            "WHERE session_id NOT IN (SELECT session_id FROM sessions)"
        )
    ]
    for chunk in _chunked(orphan_ids):
        placeholders = ",".join("?" for _ in chunk)
        conn.execute(
            f"""
            INSERT OR IGNORE INTO native_refresh_queue (session_id)
            SELECT DISTINCT other.session_id
            FROM message_claims AS mine
            CROSS JOIN message_claims AS other ON other.claim_key = mine.claim_key
            WHERE mine.session_id IN ({placeholders})
              AND other.session_id IN (SELECT session_id FROM sessions)
            """,
            chunk,
        )
        conn.execute(
            f"DELETE FROM message_claims WHERE session_id IN ({placeholders})",
            chunk,
        )


def _repair_legacy_rows(conn: sqlite3.Connection) -> None:
    """Heal row states that only older Logpile versions wrote.

    Each statement is guarded by its own WHERE clause, so on a database that
    needs nothing they read without writing; the version gate in migrate_db
    keeps them from rereading every table on every sync.
    """
    # Before explicit raw thread fields existed, Codex stored the thread UUID
    # in parent_session_id. Preserve that only lineage evidence when it is not
    # already an exact canonical session key; the sync resolver will either
    # map it after identity backfill or leave canonical parent_session_id NULL.
    conn.execute(
        """
        UPDATE sessions AS child
        SET parent_thread_id = child.parent_session_id
        WHERE child.source = 'codex'
          AND child.parent_thread_id IS NULL
          AND child.parent_session_id IS NOT NULL
          AND child.parent_session_id != ''
          AND NOT EXISTS (
              SELECT 1 FROM sessions AS parent
              WHERE parent.session_id = child.parent_session_id
          )
        """
    )
    _drop_orphan_message_claims(conn)
    for table in ("sessions", "session_daily_usage", "message_claims"):
        conn.execute(
            f"""
            UPDATE {table}
            SET cache_creation_unknown_input_tokens = CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_input_tokens
                         - cache_creation_5m_input_tokens
                         - cache_creation_1h_input_tokens
                    ELSE cache_creation_input_tokens
                END,
                cache_creation_5m_input_tokens = CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_5m_input_tokens ELSE 0
                END,
                cache_creation_1h_input_tokens = CASE
                    WHEN cache_creation_5m_input_tokens + cache_creation_1h_input_tokens
                         <= cache_creation_input_tokens
                    THEN cache_creation_1h_input_tokens ELSE 0
                END
            WHERE cache_creation_5m_input_tokens
                + cache_creation_1h_input_tokens
                + cache_creation_unknown_input_tokens
                != cache_creation_input_tokens
            """
        )
    for table in ("sessions", "session_daily_usage"):
        conn.execute(
            f"""
            UPDATE {table}
            SET native_cache_creation_unknown_input_tokens = CASE
                    WHEN native_cache_creation_5m_input_tokens
                       + native_cache_creation_1h_input_tokens
                         <= native_cache_creation_input_tokens
                    THEN native_cache_creation_input_tokens
                       - native_cache_creation_5m_input_tokens
                       - native_cache_creation_1h_input_tokens
                    ELSE native_cache_creation_input_tokens
                END,
                native_cache_creation_5m_input_tokens = CASE
                    WHEN native_cache_creation_5m_input_tokens
                       + native_cache_creation_1h_input_tokens
                         <= native_cache_creation_input_tokens
                    THEN native_cache_creation_5m_input_tokens ELSE 0
                END,
                native_cache_creation_1h_input_tokens = CASE
                    WHEN native_cache_creation_5m_input_tokens
                       + native_cache_creation_1h_input_tokens
                         <= native_cache_creation_input_tokens
                    THEN native_cache_creation_1h_input_tokens ELSE 0
                END
            WHERE native_cache_creation_5m_input_tokens
                + native_cache_creation_1h_input_tokens
                + native_cache_creation_unknown_input_tokens
                != native_cache_creation_input_tokens
            """
        )
    # Give pre-claims rows sane native values immediately (mirror transcript
    # totals) so aggregate readers never see zeros between the column
    # migration and the first full re-parse.
    _refresh_native_mirror(conn)


def _guarded_update(
    conn: sqlite3.Connection,
    table: str,
    assignments: dict[str, str],
    params: dict[str, object] | None = None,
) -> None:
    """UPDATE only the rows where some assignment would change a value.

    Each expression is evaluated against the pre-update row in both SET and
    WHERE, so a row is written exactly when at least one of its assigned
    columns would change. An UPDATE that rewrites an indexed column with its
    current value still rewrites that index entry, so the guard is what
    makes a repeated migration read-only.
    """
    set_sql = ",\n            ".join(
        f"{column} = {expression}" for column, expression in assignments.items()
    )
    where_sql = "\n           OR ".join(
        f"({expression}) IS NOT {column}" for column, expression in assignments.items()
    )
    conn.execute(
        f"UPDATE {table}\n        SET {set_sql}\n        WHERE {where_sql}",
        params or {},
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA schema_version").fetchone()[0])


def migrate_db(conn: sqlite3.Connection) -> None:
    """Bring any Logpile database to the current schema.

    Every sync runs this, so on a database that is already current it must
    not write: each data repair either only matches rows that need it or is
    gated on a logpile_meta version key, and views and triggers are replaced
    only when their stored definition differs.
    """
    schema_version_at_start = _schema_version(conn)
    search_index_existed = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'session_search_fts'"
        ).fetchone()
    )
    sessions_existed = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sessions'"
        ).fetchone()
    )
    legacy_missing_visibility_ids: list[str] = []
    if sessions_existed and "visibility" not in _table_columns(conn, "sessions"):
        legacy_columns = _table_columns(conn, "sessions")
        private_filter = (
            "WHERE COALESCE(is_private, 0) != 1"
            if "is_private" in legacy_columns
            else ""
        )
        legacy_missing_visibility_ids = [
            str(row[0])
            for row in conn.execute(
                f"SELECT session_id FROM sessions {private_filter} ORDER BY session_id"
            ).fetchall()
        ]
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
    # Add legacy rows closed, then explicitly audit the allowed private ->
    # unlisted migration below. Brand-new tables use the same private default.
    _ensure_column(conn, "sessions", "visibility", "TEXT NOT NULL DEFAULT 'private'")
    _ensure_column(
        conn, "sessions", "visibility_source", "TEXT NOT NULL DEFAULT 'default'"
    )
    _ensure_column(conn, "sessions", "visibility_rule_id", "INTEGER")
    _ensure_column(conn, "sessions", "visibility_reason", "TEXT")
    _ensure_column(conn, "sessions", "reviewed_sha256", "TEXT")
    _ensure_column(conn, "sessions", "reviewed_artifact_path", "TEXT")
    _ensure_column(conn, "sessions", "publication_metadata_sha256", "TEXT")
    _ensure_column(conn, "sessions", "reviewed_metadata_sha256", "TEXT")
    _ensure_column(conn, "sessions", "publication_review_id", "INTEGER")
    _ensure_column(
        conn,
        "sessions",
        "publication_state",
        "TEXT NOT NULL DEFAULT 'unreviewed'",
    )
    _ensure_column(conn, "session_paths", "repo_relative_path", "TEXT")
    _ensure_column(conn, "users", "display_name", "TEXT")
    _ensure_column(conn, "users", "bio", "TEXT")
    _ensure_column(conn, "users", "avatar_url", "TEXT")
    _ensure_column(
        conn, "users", "profile_visibility", "TEXT NOT NULL DEFAULT 'public'"
    )
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
    # Keep these idempotent guards after the dynamic identity rebuild as well;
    # very old databases may not have had the columns before this migration.
    _ensure_column(conn, "sessions", "cache_creation_input_tokens", "INTEGER DEFAULT 0")
    _ensure_column(
        conn, "sessions", "cache_creation_5m_input_tokens", "INTEGER DEFAULT 0"
    )
    _ensure_column(
        conn, "sessions", "cache_creation_1h_input_tokens", "INTEGER DEFAULT 0"
    )
    _ensure_column(
        conn, "sessions", "cache_creation_unknown_input_tokens", "INTEGER DEFAULT 0"
    )
    _ensure_column(conn, "sessions", "thread_id", "TEXT")
    _ensure_column(conn, "sessions", "parent_thread_id", "TEXT")
    _ensure_column(conn, "sessions", "identity_version", "INTEGER DEFAULT 0")
    _ensure_column(conn, "sessions", "reviewed_sha256", "TEXT")
    _ensure_column(conn, "sessions", "reviewed_artifact_path", "TEXT")
    _ensure_column(conn, "sessions", "publication_metadata_sha256", "TEXT")
    _ensure_column(conn, "sessions", "reviewed_metadata_sha256", "TEXT")
    _ensure_column(conn, "sessions", "publication_review_id", "INTEGER")
    _ensure_column(
        conn,
        "sessions",
        "publication_state",
        "TEXT NOT NULL DEFAULT 'unreviewed'",
    )
    _ensure_column(conn, "publication_reviews", "reviewed_metadata_sha256", "TEXT")
    _ensure_column(conn, "sessions", "file_size", "INTEGER")
    _ensure_column(conn, "sessions", "file_mtime", "REAL")
    # Set by sync when a full parse recomputes a row's structural fields; see
    # sync.SESSION_STRUCTURE_VERSION.
    _ensure_column(conn, "sessions", "structure_version", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(
        conn,
        "session_daily_usage",
        "cache_creation_unknown_input_tokens",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(
        conn, "session_daily_usage", "approximated", "INTEGER NOT NULL DEFAULT 0"
    )
    for native_column, _transcript_column in NATIVE_TOKEN_COLUMNS:
        _ensure_column(conn, "sessions", native_column, "INTEGER DEFAULT 0")
        _ensure_column(
            conn, "session_daily_usage", native_column, "INTEGER NOT NULL DEFAULT 0"
        )
    _migrate_message_claim_occurrences(conn)
    # A migration that just changed the schema is upgrading an older database,
    # so it gets every repair again, exactly as its first run always did.
    if (
        get_meta(conn, _DATA_REPAIR_META_KEY) != str(DATA_REPAIR_VERSION)
        or _schema_version(conn) != schema_version_at_start
    ):
        _repair_legacy_rows(conn)
        set_meta(conn, _DATA_REPAIR_META_KEY, str(DATA_REPAIR_VERSION))
    _guarded_update(
        conn,
        "sessions",
        {
            "workspace_root": """CASE
            WHEN workspace_root IS NOT NULL AND workspace_root != '' THEN workspace_root
            WHEN project LIKE '/%' OR project LIKE '~/%' THEN project
            ELSE workspace_root
        END"""
        },
    )
    _guarded_update(
        conn,
        "sessions",
        {
            "worktree_root": """CASE
            WHEN worktree_root IS NOT NULL AND worktree_root != '' THEN worktree_root
            WHEN workspace_root IS NOT NULL AND workspace_root != '' THEN workspace_root
            ELSE worktree_root
        END"""
        },
    )
    # Each of these expressions reads only its own column, so one guarded
    # statement (one pass over sessions) has the effect of running them one
    # at a time.
    _guarded_update(
        conn,
        "sessions",
        {
            **{
                column: f"COALESCE({column}, 0)"
                for column in (
                    "git_dirty",
                    "activity_version",
                    "narrative_version",
                    "objective_version",
                    "origin_version",
                )
            },
            "session_status": """CASE
            WHEN session_status IN ('exploration', 'success', 'partial', 'failed')
            THEN session_status
            ELSE 'exploration'
        END""",
            "session_origin": f"""CASE
            WHEN session_origin IN ({", ".join(repr(origin) for origin in SESSION_ORIGINS)})
            THEN session_origin
            ELSE 'human_direct'
        END""",
        },
    )
    # Repair legacy flags and invalid stored values through the same guarded
    # API as live commands. This keeps even migration-time tightening audited.
    # The WHERE clause selects a superset of the rows the loop body repairs
    # (every row whose stored pair is not already canonical), so the loop no
    # longer reads every session into Python on every migration.
    for visibility_row in conn.execute(
        """
        SELECT session_id, visibility, is_private
        FROM sessions
        WHERE visibility IS NULL
           OR visibility NOT IN ('private', 'unlisted', 'public')
           OR COALESCE(is_private, 0)
              != CASE WHEN visibility = 'private' THEN 1 ELSE 0 END
        ORDER BY session_id
        """
    ).fetchall():
        raw_visibility = visibility_row[1]
        is_private = visibility_row[2]
        normalized_visibility = (
            str(raw_visibility).strip().lower() if raw_visibility is not None else ""
        )
        if is_private == 1:
            desired_visibility = "private"
        elif normalized_visibility in SESSION_VISIBILITIES:
            desired_visibility = normalized_visibility
        else:
            desired_visibility = "private"
        expected_private = 1 if desired_visibility == "private" else 0
        if (
            raw_visibility != desired_visibility
            or (is_private or 0) != expected_private
        ):
            transition_session_visibility(
                conn,
                visibility_row[0],
                desired_visibility,
                shared_dir=None,
                transition_source="migration",
                reason="legacy visibility normalized closed",
                manage_storage=False,
            )
    _guarded_update(
        conn,
        "sessions",
        {
            "visibility_source": """CASE
                WHEN visibility_source IN ('manual', 'rule', 'default', 'marker', 'drift', 'migration')
                THEN visibility_source
                WHEN visibility IN ('private', 'unlisted') OR is_private = 1 THEN 'manual'
                ELSE 'default'
            END""",
            "visibility_rule_id": """CASE
                WHEN visibility_source = 'rule' THEN visibility_rule_id
                ELSE NULL
            END""",
            "visibility_reason": """CASE
                WHEN visibility_reason IS NOT NULL AND visibility_reason != '' THEN visibility_reason
                WHEN visibility IN ('private', 'unlisted') OR is_private = 1 THEN 'legacy manual'
                ELSE 'legacy default'
            END""",
        },
    )
    _guarded_update(
        conn,
        "users",
        {
            "display_name": "COALESCE(NULLIF(display_name, ''), username)",
            "profile_visibility": """CASE
                WHEN profile_visibility IN ('private', 'unlisted', 'public') THEN profile_visibility
                ELSE 'private'
            END""",
            "default_session_visibility": """CASE
                WHEN default_session_visibility IN ('private', 'unlisted', 'public')
                THEN default_session_visibility
                ELSE 'unlisted'
            END""",
            "created_at": "COALESCE(NULLIF(created_at, ''), :created_at)",
            "updated_at": "COALESCE(NULLIF(updated_at, ''), :updated_at)",
        },
        {"created_at": _now_iso(), "updated_at": _now_iso()},
    )
    # Only a handful of distinct usernames exist, and idx_sessions_username
    # lists them without reading session rows; rows are rewritten only for a
    # spelling that normalization would change.
    for (raw_username,) in conn.execute(
        "SELECT DISTINCT username FROM sessions"
    ).fetchall():
        normalized_username = normalize_username(raw_username or "")
        if normalized_username != raw_username:
            conn.execute(
                "UPDATE sessions SET username = ? WHERE username IS ?",
                (normalized_username, raw_username),
            )
    for session_id in legacy_missing_visibility_ids:
        transition_session_visibility(
            conn,
            session_id,
            "unlisted",
            shared_dir=None,
            transition_source="migration",
            reason="legacy schema lacked an explicit visibility value",
            manage_storage=False,
        )
    _guarded_update(
        conn,
        "sessions",
        {
            "publication_state": """CASE
            WHEN reviewed_sha256 IS NOT NULL
             AND reviewed_artifact_path IS NOT NULL
             AND file_hash IS NOT NULL
             AND substr(reviewed_sha256, 1, length(file_hash)) = file_hash
             AND reviewed_metadata_sha256 IS NOT NULL
             AND reviewed_metadata_sha256 = publication_metadata_sha256
            THEN 'reviewed'
            WHEN reviewed_sha256 IS NOT NULL
             AND reviewed_artifact_path IS NOT NULL
            THEN 'source_drift'
            WHEN visibility = 'private' THEN 'revoked'
            ELSE 'unreviewed'
        END"""
        },
    )
    # Legacy public rows have no enforceable review record.  Migrate them
    # closed to local/link-only unlisted through the same audited guard used
    # by live commands and rules.
    from .publish import publication_metadata_sha256

    for public_row in conn.execute(
        """
        SELECT s.*, COALESCE(u.display_name, u.username, s.username) AS display_name,
               u.bio AS bio, u.avatar_url AS avatar_url
        FROM sessions s
        LEFT JOIN users u ON u.username = s.username
        WHERE s.visibility = 'public'
        """
    ).fetchall():
        current_metadata_sha256 = publication_metadata_sha256(public_row)
        conn.execute(
            "UPDATE sessions SET publication_metadata_sha256 = ? "
            "WHERE session_id = ? AND publication_metadata_sha256 IS NOT ?",
            (
                current_metadata_sha256,
                public_row["session_id"],
                current_metadata_sha256,
            ),
        )
    legacy_public_ids = [
        row[0]
        for row in conn.execute(
            """
            SELECT s.session_id
            FROM sessions s
            WHERE s.visibility = 'public'
              AND NOT EXISTS (
                SELECT 1
                FROM publication_reviews r
                WHERE r.id = s.publication_review_id
                  AND r.session_id = s.session_id
                  AND r.successful = 1
                  AND r.approved_visibility = 'public'
                  AND r.reviewed_sha256 = s.reviewed_sha256
                  AND s.file_hash IS NOT NULL
                  AND substr(r.reviewed_sha256, 1, length(s.file_hash)) = s.file_hash
                  AND r.reviewed_artifact_path = s.reviewed_artifact_path
                  AND r.reviewed_metadata_sha256 = s.reviewed_metadata_sha256
                  AND s.reviewed_metadata_sha256 = s.publication_metadata_sha256
              )
            """
        ).fetchall()
    ]
    for session_id in legacy_public_ids:
        transition_session_visibility(
            conn,
            session_id,
            "public",
            shared_dir=None,
            transition_source="migration",
            reason="legacy public visibility lacked a verified review record",
            public_without_review="unlisted",
            manage_storage=False,
        )
        conn.execute(
            """
            UPDATE sessions
            SET publication_state = CASE
                WHEN reviewed_sha256 IS NOT NULL
                 AND reviewed_artifact_path IS NOT NULL
                THEN 'source_drift'
                ELSE 'unreviewed'
            END
            WHERE session_id = ?
            """,
            (session_id,),
        )
    conn.executescript(INDEXES)
    _replace_drifted_schema_objects(conn, "view")
    # Create search triggers only after the optional legacy identity rebuild,
    # which can replace the sessions table and drop table-bound triggers.
    search_generation = get_meta(conn, "search_fts_generation")
    reset_search_storage = not search_index_existed or search_generation != str(
        SEARCH_INDEX_VERSION
    )
    if reset_search_storage:
        # Make the reset intent durable before touching the external-content
        # table. If creation is interrupted, the next migration sees this
        # marker and resets again instead of trusting complete session states
        # beside an empty FTS term index. Canonical search data is derived, so
        # discard it and let the bounded sync backfill rebuild session by
        # session rather than running one uninterruptible whole-index rebuild.
        set_meta(conn, "search_fts_generation", f"rebuilding:{SEARCH_INDEX_VERSION}")
        set_meta(conn, "search_refresh_pending", "1")
        conn.commit()
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS session_search_documents_ai;
            DROP TRIGGER IF EXISTS session_search_documents_ad;
            DROP TRIGGER IF EXISTS session_search_documents_au;
            DROP TRIGGER IF EXISTS sessions_search_cleanup;
            DROP TRIGGER IF EXISTS sessions_search_stale;
            DROP TABLE IF EXISTS session_search_fts;
            DROP TABLE IF EXISTS session_search_documents;
            DROP TABLE IF EXISTS session_search_state;
            """
        )
    conn.executescript(SEARCH_SCHEMA)
    conn.executescript(SESSION_TRIGGERS)
    _replace_drifted_schema_objects(conn, "trigger")
    _ensure_column(conn, "session_search_state", "artifact_hash", "TEXT")
    from .search import ensure_search_checkpoint_columns  # search imports db

    ensure_search_checkpoint_columns(conn)
    set_meta(conn, "search_fts_generation", str(SEARCH_INDEX_VERSION))
    search_version = get_meta(conn, "search_index_version")
    if reset_search_storage or search_version != str(SEARCH_INDEX_VERSION):
        set_meta(conn, "search_refresh_pending", "1")


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
                self._finish_storage_transitions(transitions, "rollback", reverse=True)
            raise

        transitions = self._take_storage_transitions()
        self._finish_storage_transitions(transitions, "commit")

    def rollback(self) -> None:
        transitions = self._take_storage_transitions()
        try:
            super().rollback()
        finally:
            self._finish_storage_transitions(transitions, "rollback", reverse=True)


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
        "cache_creation_unknown_input_tokens": 0,
        "reasoning_output_tokens": 0,
        "token_version": 0,
        "thread_id": None,
        "parent_thread_id": None,
        "parent_session_id": None,
        "spawn_depth": 0,
        "identity_version": 0,
        "visibility": "private",
        "visibility_source": "default",
        "visibility_rule_id": None,
        "visibility_reason": "initial private guard",
        "is_private": 1,
        "file_size": None,
        "file_mtime": None,
        **data,
    }
    if payload.get("username"):
        payload["username"] = normalize_username(str(payload["username"]))
    desired_visibility = _normalize_visibility(
        payload.get("visibility"), SESSION_VISIBILITIES
    )
    desired_source = (payload.get("visibility_source") or "default").strip().lower()
    if desired_source not in VISIBILITY_SOURCES:
        raise ValueError(f"Unsupported visibility transition source: {desired_source}")
    desired_rule_id = payload.get("visibility_rule_id")
    desired_reason = (
        payload.get("visibility_reason") or f"{desired_source}:{desired_visibility}"
    )
    existing_visibility = conn.execute(
        """
        SELECT visibility, visibility_source, reviewed_sha256,
               reviewed_artifact_path, reviewed_metadata_sha256,
               publication_review_id, file_hash
        FROM sessions WHERE session_id = ?
        """,
        (payload["session_id"],),
    ).fetchone()
    preserve_manual = bool(
        existing_visibility
        and (existing_visibility["visibility_source"] or "default") == "manual"
    )
    # Metadata upserts do not get to mutate visibility directly.  The guarded
    # API below performs the effective transition after the row exists.
    if existing_visibility:
        payload["visibility"] = _normalize_visibility(
            existing_visibility["visibility"], SESSION_VISIBILITIES
        )
        payload["visibility_source"] = (
            existing_visibility["visibility_source"] or "default"
        )
    else:
        payload["visibility"] = "private"
        payload["visibility_source"] = "default"
    payload["visibility_rule_id"] = None
    payload["visibility_reason"] = (
        "initial private guard"
        if not existing_visibility
        else (payload.get("visibility_reason") or "preserved during metadata sync")
    )
    payload["is_private"] = 1 if payload["visibility"] == "private" else 0
    cache_creation = max(0, int(payload.get("cache_creation_input_tokens", 0) or 0))
    cache_5m = max(0, int(payload.get("cache_creation_5m_input_tokens", 0) or 0))
    cache_1h = max(0, int(payload.get("cache_creation_1h_input_tokens", 0) or 0))
    cache_unknown = max(
        0, int(payload.get("cache_creation_unknown_input_tokens", 0) or 0)
    )
    if cache_5m + cache_1h > cache_creation:
        cache_5m = cache_1h = 0
        cache_unknown = cache_creation
    elif cache_5m + cache_1h + cache_unknown != cache_creation:
        cache_unknown = cache_creation - cache_5m - cache_1h
    payload.update(
        {
            "cache_creation_input_tokens": cache_creation,
            "cache_creation_5m_input_tokens": cache_5m,
            "cache_creation_1h_input_tokens": cache_1h,
            "cache_creation_unknown_input_tokens": cache_unknown,
        }
    )
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
             cache_creation_unknown_input_tokens,
             reasoning_output_tokens, token_version, first_user_message, thread_id, parent_thread_id,
             parent_session_id, spawn_depth, identity_version, visibility,
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
             :cache_creation_unknown_input_tokens,
             :reasoning_output_tokens, :token_version, :first_user_message, :thread_id, :parent_thread_id,
             :parent_session_id, :spawn_depth, :identity_version, :visibility,
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
            cache_creation_unknown_input_tokens = excluded.cache_creation_unknown_input_tokens,
            reasoning_output_tokens = excluded.reasoning_output_tokens,
            token_version = excluded.token_version,
            first_user_message = excluded.first_user_message,
            thread_id = excluded.thread_id,
            parent_thread_id = excluded.parent_thread_id,
            parent_session_id = excluded.parent_session_id,
            spawn_depth = excluded.spawn_depth,
            identity_version = excluded.identity_version,
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
    incoming_hash = payload.get("file_hash")
    from .publish import publication_metadata_sha256

    current_publication_row = _publication_session_row(conn, payload["session_id"])
    current_metadata_sha256 = publication_metadata_sha256(current_publication_row)
    conn.execute(
        "UPDATE sessions SET publication_metadata_sha256 = ? WHERE session_id = ?",
        (current_metadata_sha256, payload["session_id"]),
    )
    revokes_drifted_publication = bool(
        existing_visibility
        and existing_visibility["visibility"] == "public"
        and existing_visibility["reviewed_sha256"]
        and existing_visibility["reviewed_artifact_path"]
        and (
            not incoming_hash
            or not str(existing_visibility["reviewed_sha256"]).startswith(
                str(incoming_hash)
            )
        )
        or (
            existing_visibility
            and existing_visibility["visibility"] == "public"
            and existing_visibility["reviewed_metadata_sha256"]
            and existing_visibility["reviewed_metadata_sha256"]
            != current_metadata_sha256
        )
    )
    if revokes_drifted_publication:
        transition_session_visibility(
            conn,
            payload["session_id"],
            "unlisted",
            shared_dir=None,
            transition_source="drift",
            reason="source revision drifted from reviewed artifact; publication revoked and requeued",
            manage_storage=False,
        )
        conn.execute(
            """
            UPDATE sessions
            SET publication_state = 'source_drift',
                visibility_reason = 'source revision drifted from reviewed artifact; publication revoked and requeued'
            WHERE session_id = ?
            """,
            (payload["session_id"],),
        )
    elif not preserve_manual:
        result = transition_session_visibility(
            conn,
            payload["session_id"],
            desired_visibility,
            shared_dir=None,
            transition_source=desired_source,
            reason=str(desired_reason),
            visibility_rule_id=desired_rule_id,
            public_without_review="unlisted",
            manage_storage=False,
        )
        if result.warning and desired_visibility == "public":
            warnings.warn(result.warning, RuntimeWarning, stacklevel=2)


# Columns of session_daily_usage derived from the transcript (everything
# except the key and the native_* columns refresh_native_usage owns).
_DAILY_VALUE_COLUMNS = (
    "total_input_tokens",
    "total_output_tokens",
    "fresh_input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "reasoning_output_tokens",
    "user_message_count",
    "assistant_message_count",
    "tool_call_count",
    "approximated",
)
_INSERT_DAILY_SQL = f"""
    INSERT INTO session_daily_usage (session_id, day, {", ".join(_DAILY_VALUE_COLUMNS)})
    VALUES (?, ?, {", ".join("?" for _ in _DAILY_VALUE_COLUMNS)})
"""


def _daily_values(day) -> tuple:
    """The stored transcript-derived values of one DailyUsage slice."""
    return (
        *(getattr(day, column) for column in _DAILY_VALUE_COLUMNS[:-1]),
        1 if day.approximated else 0,
    )


def insert_session_daily_usage(conn, session_id: str, daily_usage: list):
    """Make the session's day rows match ``daily_usage``, writing only changes.

    The end state equals deleting every row and inserting ``daily_usage``,
    except that native_* on surviving rows keeps its value until the next
    refresh_native_usage for the session: a day whose transcript columns are
    unchanged is not written, a changed day is updated in place, and a day
    that vanished is deleted.
    """
    daily_usage = list(daily_usage)
    session_row = conn.execute(
        """
        SELECT total_input_tokens, total_output_tokens, fresh_input_tokens,
               cached_input_tokens, cache_creation_input_tokens,
               cache_creation_5m_input_tokens, cache_creation_1h_input_tokens,
               cache_creation_unknown_input_tokens, reasoning_output_tokens,
               user_message_count, assistant_message_count, tool_call_count
        FROM sessions WHERE session_id = ?
        """,
        (session_id,),
    ).fetchone()
    component_fields = (
        "total_input_tokens",
        "total_output_tokens",
        "fresh_input_tokens",
        "cached_input_tokens",
        "cache_creation_input_tokens",
        "cache_creation_5m_input_tokens",
        "cache_creation_1h_input_tokens",
        "cache_creation_unknown_input_tokens",
        "reasoning_output_tokens",
        "user_message_count",
        "assistant_message_count",
        "tool_call_count",
    )
    if session_row is not None:
        mismatches = {
            field: (
                sum(int(getattr(day, field, 0) or 0) for day in daily_usage),
                int(session_row[field] or 0),
            )
            for field in component_fields
            if sum(int(getattr(day, field, 0) or 0) for day in daily_usage)
            != int(session_row[field] or 0)
        }
        if mismatches:
            raise ValueError(
                f"daily usage does not reconcile for session {session_id}: {mismatches}"
            )
    for day in daily_usage:
        cache_creation = int(day.cache_creation_input_tokens or 0)
        split = (
            int(day.cache_creation_5m_input_tokens or 0)
            + int(day.cache_creation_1h_input_tokens or 0)
            + int(day.cache_creation_unknown_input_tokens or 0)
        )
        if split != cache_creation:
            raise ValueError(
                f"cache-creation daily split does not reconcile for {session_id} "
                f"on {day.day}: {split} != {cache_creation}"
            )
    desired = {day.day: _daily_values(day) for day in daily_usage}
    if len(desired) != len(daily_usage):
        # Two slices for one day violate the (session_id, day) key. Take the
        # original delete-and-insert path so the same IntegrityError surfaces.
        conn.execute(
            "DELETE FROM session_daily_usage WHERE session_id = ?", (session_id,)
        )
        conn.executemany(
            _INSERT_DAILY_SQL,
            [(session_id, day.day, *_daily_values(day)) for day in daily_usage],
        )
        return
    # native_* is left alone: sync recomputes it with refresh_native_usage for
    # every session it writes, and that recomputes every native column of
    # every day row of the session from claims or transcript totals.
    stored = {
        row[0]: tuple(row[1:])
        for row in conn.execute(
            f"SELECT day, {', '.join(_DAILY_VALUE_COLUMNS)} "
            "FROM session_daily_usage WHERE session_id = ?",
            (session_id,),
        )
    }
    vanished = [(session_id, day) for day in stored if day not in desired]
    if vanished:
        conn.executemany(
            "DELETE FROM session_daily_usage WHERE session_id = ? AND day = ?",
            vanished,
        )
    changed = [
        (*values, session_id, day)
        for day, values in desired.items()
        if day in stored and stored[day] != values
    ]
    if changed:
        assignments = ", ".join(f"{column} = ?" for column in _DAILY_VALUE_COLUMNS)
        conn.executemany(
            f"UPDATE session_daily_usage SET {assignments} "
            "WHERE session_id = ? AND day = ?",
            changed,
        )
    added = [
        (session_id, day, *values)
        for day, values in desired.items()
        if day not in stored
    ]
    if added:
        conn.executemany(_INSERT_DAILY_SQL, added)


def _chunked(values: list, size: int = 500):
    for start in range(0, len(values), size):
        yield values[start : start + size]


def _session_rank(last_timestamp, first_timestamp, session_id) -> tuple[str, str, str]:
    """Ownership rank; the minimum-ranked claimant owns a message.

    Earliest last_timestamp wins: a resume transcript is a superset of its
    source, so it always ends at or after the source — the earliest-ending
    claimant is the session where the message actually ran. Sessions without
    timestamps rank after any dated session ("~" > every digit in ASCII).
    """
    return (last_timestamp or "~", first_timestamp or "~", session_id)


# Every session holding a claim on a touched key. Driving the join from the
# small temp table turns each key into a (claim_key, session_id) primary-key
# range search; left to itself, SQLite (no sqlite_stat1) scanned the whole
# claims index once per call instead.
_TOUCHED_CLAIMANTS_SQL = """
    SELECT DISTINCT claims.session_id
    FROM _logpile_touched_message_claims AS touched
    CROSS JOIN message_claims AS claims ON claims.claim_key = touched.claim_key
"""

_CLAIM_VALUE_COLUMNS = (
    "day",
    "model",
    "fresh_input_tokens",
    "cached_input_tokens",
    "cache_creation_input_tokens",
    "cache_creation_5m_input_tokens",
    "cache_creation_1h_input_tokens",
    "cache_creation_unknown_input_tokens",
    "output_tokens",
)


def apply_message_claims(conn, session_id: str, message_usage) -> set[str]:
    """Replace one session's occurrences and return every possibly stale owner.

    Losing occurrences remain in the ledger. The `message_claim_owners` view
    derives the minimum-ranked live claimant from all rows, so a reparse that
    drops a winning key or changes a session rank immediately promotes an
    unchanged loser. Returning every claimant for touched keys makes the
    scoped native refresh correct before and after any such ownership change.

    Only claim rows whose values change are written: an identical occurrence
    is neither updated nor deleted and reinserted.
    """
    # Stage the current iterable in SQLite rather than converting it to a
    # list/set. Claude's parser deliberately returns a disk-backed reusable
    # sequence, and materializing it here would restore output-proportional
    # heap usage during sync.
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _logpile_current_message_claims (
            claim_key TEXT PRIMARY KEY,
            day TEXT,
            model TEXT,
            fresh_input_tokens INTEGER NOT NULL,
            cached_input_tokens INTEGER NOT NULL,
            cache_creation_input_tokens INTEGER NOT NULL,
            cache_creation_5m_input_tokens INTEGER NOT NULL,
            cache_creation_1h_input_tokens INTEGER NOT NULL,
            cache_creation_unknown_input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL
        ) WITHOUT ROWID
        """
    )
    conn.execute(
        """
        CREATE TEMP TABLE IF NOT EXISTS _logpile_touched_message_claims (
            claim_key TEXT PRIMARY KEY
        ) WITHOUT ROWID
        """
    )
    conn.execute("DELETE FROM _logpile_current_message_claims")
    conn.execute("DELETE FROM _logpile_touched_message_claims")

    def normalized_rows():
        for message in message_usage:
            total = max(0, int(message.cache_creation_input_tokens or 0))
            cache_5m = max(0, int(message.cache_creation_5m_input_tokens or 0))
            cache_1h = max(0, int(message.cache_creation_1h_input_tokens or 0))
            cache_unknown = max(
                0,
                int(
                    getattr(
                        message,
                        "cache_creation_unknown_input_tokens",
                        0,
                    )
                    or 0
                ),
            )
            if cache_5m + cache_1h > total:
                cache_5m = cache_1h = 0
                cache_unknown = total
            elif cache_5m + cache_1h + cache_unknown != total:
                cache_unknown = total - cache_5m - cache_1h
            yield (
                message.claim_key,
                message.day,
                message.model,
                message.fresh_input_tokens,
                message.cached_input_tokens,
                total,
                cache_5m,
                cache_1h,
                cache_unknown,
                message.output_tokens,
            )

    conn.executemany(
        """
        INSERT INTO _logpile_current_message_claims (
            claim_key, day, model, fresh_input_tokens,
            cached_input_tokens, cache_creation_input_tokens,
            cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens, output_tokens
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(claim_key) DO UPDATE SET
            day = excluded.day,
            model = excluded.model,
            fresh_input_tokens = excluded.fresh_input_tokens,
            cached_input_tokens = excluded.cached_input_tokens,
            cache_creation_input_tokens = excluded.cache_creation_input_tokens,
            cache_creation_5m_input_tokens =
                excluded.cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens =
                excluded.cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens =
                excluded.cache_creation_unknown_input_tokens,
            output_tokens = excluded.output_tokens
        """,
        normalized_rows(),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO _logpile_touched_message_claims (claim_key)
        SELECT claim_key FROM message_claims WHERE session_id = ?
        """,
        (session_id,),
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO _logpile_touched_message_claims (claim_key)
        SELECT claim_key FROM _logpile_current_message_claims
        """
    )
    if (
        conn.execute("SELECT 1 FROM _logpile_touched_message_claims LIMIT 1").fetchone()
        is None
    ):
        return set()

    # Claimants are read once, before this session's rows change. The only
    # rows written below belong to session_id, which is already in the set,
    # so a second read afterwards (as this code once did) finds nobody new:
    # another session claims a touched key after the write exactly when it
    # did before.
    affected: set[str] = {session_id}
    affected.update(row[0] for row in conn.execute(_TOUCHED_CLAIMANTS_SQL))
    update_set = ",\n            ".join(
        f"{column} = excluded.{column}" for column in _CLAIM_VALUE_COLUMNS
    )
    changed = "\n           OR ".join(
        f"message_claims.{column} IS NOT excluded.{column}"
        for column in _CLAIM_VALUE_COLUMNS
    )
    conn.execute(
        f"""
        INSERT INTO message_claims (
            claim_key, session_id, day, model,
            fresh_input_tokens, cached_input_tokens,
            cache_creation_input_tokens, cache_creation_5m_input_tokens,
            cache_creation_1h_input_tokens,
            cache_creation_unknown_input_tokens, output_tokens
        )
        SELECT claim_key, ?, day, model,
               fresh_input_tokens, cached_input_tokens,
               cache_creation_input_tokens, cache_creation_5m_input_tokens,
               cache_creation_1h_input_tokens,
               cache_creation_unknown_input_tokens, output_tokens
        FROM _logpile_current_message_claims
        WHERE 1
        ON CONFLICT(claim_key, session_id) DO UPDATE SET
            {update_set}
        WHERE {changed}
        """,
        (session_id,),
    )
    conn.execute(
        """
        DELETE FROM message_claims
        WHERE session_id = ?
          AND NOT EXISTS (
              SELECT 1 FROM _logpile_current_message_claims AS current
              WHERE current.claim_key = message_claims.claim_key
          )
        """,
        (session_id,),
    )
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
    assignments = ", ".join(
        f"{native} = {transcript}" for native, transcript in NATIVE_TOKEN_COLUMNS
    )
    drift_guard = " OR ".join(
        f"{native} != {transcript}" for native, transcript in NATIVE_TOKEN_COLUMNS
    )
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
        COALESCE(SUM(c.cache_creation_unknown_input_tokens), 0),
        0,
        COUNT(c.claim_key)
    """
    scope, params = _native_scope_clause(session_ids)
    conn.execute(
        f"""
        UPDATE sessions SET ({native_columns}) = (
            SELECT {aggregates}
            FROM message_claim_owners o
            JOIN message_claims c
              ON c.claim_key = o.claim_key
             AND c.session_id = o.owner_session_id
            WHERE o.owner_session_id = sessions.session_id
        )
        WHERE source = 'claudecode' AND COALESCE(token_version, 0) >= ?{scope}
        """,
        [CLAIMS_TOKEN_VERSION, *params],
    )
    conn.execute(
        f"""
        UPDATE session_daily_usage SET ({native_columns}) = (
            SELECT {aggregates}
            FROM message_claim_owners o
            JOIN message_claims c
              ON c.claim_key = o.claim_key
             AND c.session_id = o.owner_session_id
            WHERE o.owner_session_id = session_daily_usage.session_id
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
    A refreshed session no longer owes anything, so its native_refresh_queue
    entry is removed in the same transaction (every entry, for None).
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
        if chunk is None:
            conn.execute("DELETE FROM native_refresh_queue")
        else:
            placeholders = ",".join("?" for _ in chunk)
            conn.execute(
                f"DELETE FROM native_refresh_queue WHERE session_id IN ({placeholders})",
                sorted(chunk),
            )


def queue_native_refresh(conn, session_ids: Iterable[str]) -> None:
    """Durably record that these sessions' native_* columns must be recomputed.

    The entries commit with the caller's transaction and stay until
    refresh_native_usage (or drain_native_refresh) recomputes the session or
    the session row is deleted.
    """
    if isinstance(session_ids, str):
        raise TypeError("queue_native_refresh takes an iterable of session ids")
    conn.executemany(
        "INSERT OR IGNORE INTO native_refresh_queue (session_id) VALUES (?)",
        ((session_id,) for session_id in session_ids),
    )


def _adopt_legacy_native_refresh_flag(conn) -> None:
    """Turn an older sync's pending full-refresh flag into queue entries.

    Before native_refresh_queue, a sync set native_refresh_pending='1' when it
    started and '0' once its refresh finished, so anything but '0' means a
    sync died and every session's native_* is suspect. Enqueue them all once
    and clear the flag; a flag that stays '0' asks for nothing.
    """
    if get_meta(conn, _LEGACY_NATIVE_REFRESH_FLAG) in (None, "0"):
        return
    conn.execute(
        "INSERT OR IGNORE INTO native_refresh_queue (session_id) "
        "SELECT session_id FROM sessions"
    )
    set_meta(conn, _LEGACY_NATIVE_REFRESH_FLAG, "0")


def drain_native_refresh(
    conn, *, deadline: float | None = None, chunk_size: int = 500
) -> int:
    """Refresh queued sessions chunk by chunk; return how many remain queued.

    Each chunk is recomputed with refresh_native_usage, which also removes the
    chunk's queue entries, and then committed, so an interrupted drain keeps
    every finished chunk. The commit also commits whatever the caller had
    pending on ``conn``. ``deadline`` is a time.monotonic() value; the drain
    checks it before each chunk and stops once it is reached.
    """
    if chunk_size < 1:
        raise ValueError("chunk_size must be at least 1")
    _adopt_legacy_native_refresh_flag(conn)
    while deadline is None or time.monotonic() < deadline:
        chunk = [
            row[0]
            for row in conn.execute(
                "SELECT session_id FROM native_refresh_queue ORDER BY session_id LIMIT ?",
                (chunk_size,),
            )
        ]
        if not chunk:
            break
        refresh_native_usage(conn, chunk)
        conn.commit()
    return int(conn.execute("SELECT COUNT(*) FROM native_refresh_queue").fetchone()[0])


def get_meta(conn, key: str) -> str | None:
    row = conn.execute(
        "SELECT value FROM logpile_meta WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set_meta(conn, key: str, value: str | None) -> None:
    """Store a meta value, writing nothing when it is already stored."""
    if value is None:
        conn.execute("DELETE FROM logpile_meta WHERE key = ?", (key,))
    else:
        conn.execute(
            """
            INSERT INTO logpile_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            WHERE logpile_meta.value IS NOT excluded.value
            """,
            (key, value),
        )


_TOOL_CALL_COLUMNS = ("tool_name", "command", "timestamp", "is_error")
# Stored rows are compared a page at a time; each page is fetched in full
# before any write so no cursor is open on tool_calls while it changes.
_TOOL_CALL_PAGE_ROWS = 1000


def _update_changed_columns(
    conn, table: str, columns: tuple[str, ...], changes: Iterable[tuple]
) -> None:
    """Apply (row_id, stored_values, new_values) triples column-minimally.

    Rows are grouped by which columns actually differ and each group is one
    UPDATE of just those columns, so an unchanged indexed column is never
    part of the SET list and its index entry is left alone.
    """
    groups: dict[tuple[str, ...], list[tuple]] = {}
    for row_id, stored, new in changes:
        differing = tuple(
            index
            for index, (old_value, new_value) in enumerate(
                zip(stored, new, strict=True)
            )
            if old_value != new_value
        )
        if differing:
            groups.setdefault(tuple(columns[i] for i in differing), []).append(
                (*(new[i] for i in differing), row_id)
            )
    for changed_columns, rows in groups.items():
        assignments = ", ".join(f"{column} = ?" for column in changed_columns)
        conn.executemany(f"UPDATE {table} SET {assignments} WHERE id = ?", rows)


def insert_tool_calls(conn, session_id: str, tool_calls):
    """Make the session's tool_calls rows, in id order, equal ``tool_calls``.

    Rows are matched by position: a stored row equal to the call at its
    position keeps its id and is not written, a differing one is updated in
    place, calls past the stored rows are appended (AUTOINCREMENT ids only
    grow, so they sort after every kept row), and stored rows past the last
    call are deleted. ``tool_calls`` is iterated exactly once, so a lazy,
    disk-backed sequence is streamed rather than loaded.
    """
    calls = iter(tool_calls)
    exhausted = object()
    last_id = None
    while True:
        if last_id is None:
            page = conn.execute(
                "SELECT id, tool_name, command, timestamp, is_error FROM tool_calls "
                "WHERE session_id = ? ORDER BY id LIMIT ?",
                (session_id, _TOOL_CALL_PAGE_ROWS),
            ).fetchall()
        else:
            page = conn.execute(
                "SELECT id, tool_name, command, timestamp, is_error FROM tool_calls "
                "WHERE session_id = ? AND id > ? ORDER BY id LIMIT ?",
                (session_id, last_id, _TOOL_CALL_PAGE_ROWS),
            ).fetchall()
        if not page:
            break
        changes: list[tuple] = []
        for row in page:
            call = next(calls, exhausted)
            if call is exhausted:
                _update_changed_columns(conn, "tool_calls", _TOOL_CALL_COLUMNS, changes)
                conn.execute(
                    "DELETE FROM tool_calls WHERE session_id = ? AND id >= ?",
                    (session_id, row[0]),
                )
                return
            desired = _tool_call_values(call)
            stored = tuple(row[1:])
            if stored != desired:
                changes.append((row[0], stored, desired))
        _update_changed_columns(conn, "tool_calls", _TOOL_CALL_COLUMNS, changes)
        last_id = page[-1][0]
    conn.executemany(
        "INSERT INTO tool_calls (session_id, tool_name, command, timestamp, is_error) VALUES (?,?,?,?,?)",
        ((session_id, *_tool_call_values(call)) for call in calls),
    )


def _tool_call_values(call) -> tuple:
    return (call.tool_name, call.command, call.timestamp, 1 if call.is_error else 0)


# Staging schema for insert_session_paths. ``current`` aggregates the parsed
# paths exactly as the old per-session temp table did; ``stored`` copies the
# session's existing rows under the same key, plus their ids. Both live in a
# private scratch database, not on ``conn``, so diffing writes nothing to the
# index database (and adds nothing to conn.total_changes) beyond the changes.
_SESSION_PATH_STAGING_SCHEMA = """
CREATE TABLE current (
    normalized_path TEXT NOT NULL,
    operation TEXT NOT NULL,
    source TEXT NOT NULL,
    tool_name_missing INTEGER NOT NULL,
    tool_name_key TEXT NOT NULL,
    raw_path TEXT NOT NULL,
    relative_path TEXT,
    repo_relative_path TEXT,
    display_path TEXT NOT NULL,
    first_timestamp TEXT,
    last_timestamp TEXT,
    occurrence_count INTEGER NOT NULL,
    PRIMARY KEY (
        normalized_path, operation, source,
        tool_name_missing, tool_name_key
    )
) WITHOUT ROWID;

CREATE TABLE stored (
    normalized_path TEXT NOT NULL,
    operation TEXT NOT NULL,
    source TEXT NOT NULL,
    tool_name_missing INTEGER NOT NULL,
    tool_name_key TEXT NOT NULL,
    id INTEGER NOT NULL,
    raw_path TEXT NOT NULL,
    relative_path TEXT,
    repo_relative_path TEXT,
    display_path TEXT NOT NULL,
    first_timestamp TEXT,
    last_timestamp TEXT,
    occurrence_count INTEGER NOT NULL,
    PRIMARY KEY (
        normalized_path, operation, source,
        tool_name_missing, tool_name_key, id
    )
) WITHOUT ROWID;
"""

_SESSION_PATH_VALUE_COLUMNS = (
    "raw_path",
    "relative_path",
    "repo_relative_path",
    "display_path",
    "first_timestamp",
    "last_timestamp",
    "occurrence_count",
)

_SAME_PATH_KEY = """
    {a}.normalized_path = {b}.normalized_path
    AND {a}.operation = {b}.operation
    AND {a}.source = {b}.source
    AND {a}.tool_name_missing = {b}.tool_name_missing
    AND {a}.tool_name_key = {b}.tool_name_key
"""

# The row that survives for a key is the stored row with the smallest id;
# any duplicate of a key (which current writers never produce) is deleted.
_KEPT_STORED_PATH = f"""
    stored.id = (
        SELECT MIN(twin.id) FROM stored AS twin
        WHERE {_SAME_PATH_KEY.format(a="twin", b="stored")}
    )
"""


def insert_session_paths(conn, session_id: str, session_paths):
    """Make the session's session_paths rows match ``session_paths``.

    Paths are aggregated per (normalized_path, operation, source, tool_name):
    the earliest and latest non-NULL timestamps, the occurrence count, and
    the raw/relative/repo-relative/display spelling of the first occurrence.
    The resulting rows equal what deleting and reinserting the aggregate
    would produce. Only rows whose aggregate changed are written: an
    unchanged key keeps its row and id, a changed key is updated in place
    (only its differing columns), a new key is inserted, and a vanished key
    is deleted. Row ids therefore no longer follow key order; no reader
    orders session_paths by id.
    """
    # Aggregate in SQLite so a transcript touching millions of unique paths
    # does not build an equally large Python dictionary (and then a second
    # list for executemany). ``tool_name_missing`` keeps None distinct from
    # the empty string while still giving the WITHOUT ROWID table a fully
    # non-null primary key equivalent to the old tuple key. An empty
    # filename opens a private on-disk scratch database that SQLite deletes
    # on close, so staging spills to disk instead of the heap.
    with closing(sqlite3.connect("")) as staging:
        staging.executescript(_SESSION_PATH_STAGING_SCHEMA)

        def staged_rows():
            for path in session_paths:
                missing_tool_name = 1 if path.tool_name is None else 0
                yield (
                    path.normalized_path,
                    path.operation,
                    path.source,
                    missing_tool_name,
                    "" if missing_tool_name else path.tool_name,
                    path.raw_path,
                    path.relative_path,
                    getattr(path, "repo_relative_path", None),
                    path.display_path,
                    path.timestamp,
                    path.timestamp,
                    1,
                )

        staging.executemany(
            """
            INSERT INTO current (
                normalized_path, operation, source,
                tool_name_missing, tool_name_key, raw_path,
                relative_path, repo_relative_path, display_path,
                first_timestamp, last_timestamp, occurrence_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (
                normalized_path, operation, source,
                tool_name_missing, tool_name_key
            ) DO UPDATE SET
                first_timestamp = CASE
                    WHEN excluded.first_timestamp IS NULL
                        THEN current.first_timestamp
                    WHEN current.first_timestamp IS NULL
                      OR excluded.first_timestamp < current.first_timestamp
                        THEN excluded.first_timestamp
                    ELSE current.first_timestamp
                END,
                last_timestamp = CASE
                    WHEN excluded.last_timestamp IS NULL
                        THEN current.last_timestamp
                    WHEN current.last_timestamp IS NULL
                      OR excluded.last_timestamp > current.last_timestamp
                        THEN excluded.last_timestamp
                    ELSE current.last_timestamp
                END,
                occurrence_count = current.occurrence_count + 1
            """,
            staged_rows(),
        )
        staging.executemany(
            """
            INSERT INTO stored (
                id, normalized_path, operation, source,
                tool_name_missing, tool_name_key, raw_path,
                relative_path, repo_relative_path, display_path,
                first_timestamp, last_timestamp, occurrence_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            conn.execute(
                """
                SELECT id, normalized_path, operation, source,
                       CASE WHEN tool_name IS NULL THEN 1 ELSE 0 END,
                       COALESCE(tool_name, ''), raw_path,
                       relative_path, repo_relative_path, display_path,
                       first_timestamp, last_timestamp, occurrence_count
                FROM session_paths WHERE session_id = ?
                """,
                (session_id,),
            ),
        )
        conn.executemany(
            "DELETE FROM session_paths WHERE id = ?",
            staging.execute(
                f"""
                SELECT stored.id FROM stored
                WHERE NOT ({_KEPT_STORED_PATH})
                   OR NOT EXISTS (
                       SELECT 1 FROM current
                       WHERE {_SAME_PATH_KEY.format(a="current", b="stored")}
                   )
                """
            ),
        )
        value_columns = ", ".join(
            f"current.{column}" for column in _SESSION_PATH_VALUE_COLUMNS
        )
        stored_columns = ", ".join(
            f"stored.{column}" for column in _SESSION_PATH_VALUE_COLUMNS
        )
        differs = " OR ".join(
            f"stored.{column} IS NOT current.{column}"
            for column in _SESSION_PATH_VALUE_COLUMNS
        )
        _update_changed_columns(
            conn,
            "session_paths",
            _SESSION_PATH_VALUE_COLUMNS,
            (
                (row[0], row[1 : 1 + width], row[1 + width :])
                for width in (len(_SESSION_PATH_VALUE_COLUMNS),)
                for row in staging.execute(
                    f"""
                    SELECT stored.id, {stored_columns}, {value_columns}
                    FROM stored
                    JOIN current ON {_SAME_PATH_KEY.format(a="current", b="stored")}
                    WHERE {_KEPT_STORED_PATH} AND ({differs})
                    """
                )
            ),
        )
        conn.executemany(
            """
            INSERT INTO session_paths (
                session_id, raw_path, normalized_path, relative_path,
                repo_relative_path, display_path, operation, source,
                tool_name, first_timestamp, last_timestamp, occurrence_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (session_id, *row)
                for row in staging.execute(
                    f"""
                    SELECT raw_path, normalized_path, relative_path,
                           repo_relative_path, display_path, operation, source,
                           CASE WHEN tool_name_missing = 1 THEN NULL ELSE tool_name_key END,
                           first_timestamp, last_timestamp, occurrence_count
                    FROM current
                    WHERE NOT EXISTS (
                        SELECT 1 FROM stored
                        WHERE {_SAME_PATH_KEY.format(a="stored", b="current")}
                    )
                    ORDER BY normalized_path, operation, source,
                             tool_name_missing, tool_name_key
                    """
                )
            ),
        )


def get_transcript_checkpoint(
    conn, source_path: str
) -> tuple[ParseCheckpoint, str] | None:
    """Return the stored (checkpoint, state_file) for a transcript, if any."""
    row = conn.execute(
        """
        SELECT parse_offset, parse_prefix_sha256, parse_generation,
               dev, ino, parse_version, state_file
        FROM transcript_checkpoints WHERE source_path = ?
        """,
        (source_path,),
    ).fetchone()
    if row is None:
        return None
    checkpoint = ParseCheckpoint(
        offset=int(row[0]),
        prefix_sha256=row[1],
        generation=row[2],
        dev=int(row[3]),
        ino=int(row[4]),
        version=int(row[5]),
    )
    return checkpoint, row[6]


def put_transcript_checkpoint(
    conn,
    *,
    source_path: str,
    session_id: str,
    source: str,
    checkpoint: ParseCheckpoint,
    state_file: str,
    file_mtime: float | None,
    now: str,
) -> None:
    """Insert or replace the checkpoint for ``source_path``.

    An identical stored checkpoint (``now`` aside) is left unwritten.
    """
    conn.execute(
        """
        INSERT INTO transcript_checkpoints (
            source_path, session_id, source, dev, ino,
            parse_offset, parse_prefix_sha256, parse_generation,
            parse_version, state_file, file_mtime, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_path) DO UPDATE SET
            session_id = excluded.session_id,
            source = excluded.source,
            dev = excluded.dev,
            ino = excluded.ino,
            parse_offset = excluded.parse_offset,
            parse_prefix_sha256 = excluded.parse_prefix_sha256,
            parse_generation = excluded.parse_generation,
            parse_version = excluded.parse_version,
            state_file = excluded.state_file,
            file_mtime = excluded.file_mtime,
            updated_at = excluded.updated_at
        WHERE transcript_checkpoints.session_id IS NOT excluded.session_id
           OR transcript_checkpoints.source IS NOT excluded.source
           OR transcript_checkpoints.dev IS NOT excluded.dev
           OR transcript_checkpoints.ino IS NOT excluded.ino
           OR transcript_checkpoints.parse_offset IS NOT excluded.parse_offset
           OR transcript_checkpoints.parse_prefix_sha256
              IS NOT excluded.parse_prefix_sha256
           OR transcript_checkpoints.parse_generation IS NOT excluded.parse_generation
           OR transcript_checkpoints.parse_version IS NOT excluded.parse_version
           OR transcript_checkpoints.state_file IS NOT excluded.state_file
           OR transcript_checkpoints.file_mtime IS NOT excluded.file_mtime
        """,
        (
            source_path,
            session_id,
            source,
            checkpoint.dev,
            checkpoint.ino,
            checkpoint.offset,
            checkpoint.prefix_sha256,
            checkpoint.generation,
            checkpoint.version,
            state_file,
            file_mtime,
            now,
        ),
    )


def delete_transcript_checkpoint(conn, source_path: str) -> None:
    conn.execute(
        "DELETE FROM transcript_checkpoints WHERE source_path = ?", (source_path,)
    )


_CHECKPOINT_PAGE_ROWS = 500


def iter_transcript_checkpoints(conn) -> Iterator[tuple[str, str, float | None]]:
    """Yield (source_path, state_file, file_mtime) for every checkpoint.

    Rows are read a page at a time in source_path order, with no cursor left
    open between pages, so a caller may delete checkpoints while iterating
    (garbage collection of state files does exactly that).
    """
    last_path: str | None = None
    while True:
        if last_path is None:
            page = conn.execute(
                "SELECT source_path, state_file, file_mtime FROM transcript_checkpoints "
                "ORDER BY source_path LIMIT ?",
                (_CHECKPOINT_PAGE_ROWS,),
            ).fetchall()
        else:
            page = conn.execute(
                "SELECT source_path, state_file, file_mtime FROM transcript_checkpoints "
                "WHERE source_path > ? ORDER BY source_path LIMIT ?",
                (last_path, _CHECKPOINT_PAGE_ROWS),
            ).fetchall()
        if not page:
            return
        for row in page:
            yield row[0], row[1], row[2]
        last_path = page[-1][0]
