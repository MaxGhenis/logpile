CREATE TABLE users (
    slug TEXT PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT,
    bio TEXT,
    avatar_url TEXT,
    profile_visibility TEXT NOT NULL DEFAULT 'public',
    default_session_visibility TEXT NOT NULL DEFAULT 'unlisted',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    github_username TEXT,
    custom_profile_note TEXT
);

CREATE TABLE session_visibility_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_slug TEXT NOT NULL,
    source_scope TEXT,
    field TEXT NOT NULL,
    match_mode TEXT NOT NULL,
    pattern TEXT NOT NULL,
    visibility TEXT NOT NULL DEFAULT 'public',
    priority INTEGER NOT NULL DEFAULT 100,
    threshold REAL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    custom_rule_note TEXT
);

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    username TEXT NOT NULL,
    user_slug TEXT,
    machine TEXT,
    project TEXT,
    source_path TEXT NOT NULL,
    shared_path TEXT NOT NULL,
    first_timestamp TEXT,
    last_timestamp TEXT,
    duration_seconds REAL,
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    error_count INTEGER DEFAULT 0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    cache_creation_input_tokens INTEGER DEFAULT 0,
    native_total_output_tokens INTEGER DEFAULT 0,
    first_user_message TEXT,
    visibility TEXT NOT NULL DEFAULT 'unlisted',
    visibility_source TEXT NOT NULL DEFAULT 'default',
    visibility_rule_id INTEGER,
    visibility_reason TEXT,
    is_private INTEGER DEFAULT 0,
    file_hash TEXT,
    synced_at TEXT,
    model TEXT,
    custom_session_note TEXT
);

CREATE TABLE user_github_daily (
    username TEXT NOT NULL,
    day TEXT NOT NULL,
    contributions INTEGER DEFAULT 0,
    commits INTEGER DEFAULT 0,
    prs_opened INTEGER DEFAULT 0,
    prs_reviewed INTEGER DEFAULT 0,
    issues_opened INTEGER DEFAULT 0,
    synced_at TEXT NOT NULL,
    custom_github_note TEXT,
    PRIMARY KEY (username, day)
);

INSERT INTO users (
    slug, username, display_name, profile_visibility,
    default_session_visibility, created_at, updated_at,
    github_username, custom_profile_note
) VALUES
    ('upper-slug', 'Alice', 'Upper Alice', 'public', 'unlisted',
     '2025-01-01T00:00:00Z', '2025-01-02T00:00:00Z', 'AliceUpper', 'upper-note'),
    ('lower-slug', 'alice', 'Lower Alice', 'unlisted', 'private',
     '2025-02-01T00:00:00Z', '2025-02-02T00:00:00Z', 'aliceLower', 'lower-note');

INSERT INTO sessions (
    session_id, source, username, user_slug, source_path, shared_path,
    assistant_message_count, total_input_tokens, total_output_tokens,
    cache_creation_input_tokens, native_total_output_tokens,
    visibility, custom_session_note
) VALUES
    ('session-upper', 'claudecode', 'Alice', 'upper-slug',
     '/source/upper.jsonl', '/shared/upper.jsonl', 3, 101, 11, 31, 11,
     'unlisted', 'upper-session-note'),
    ('session-lower', 'claudecode', 'alice', 'lower-slug',
     '/source/lower.jsonl', '/shared/lower.jsonl', 4, 202, 22, 32, 22,
     'private', 'lower-session-note');

INSERT INTO session_visibility_rules (
    user_slug, source_scope, field, match_mode, pattern, visibility,
    priority, enabled, created_at, updated_at, custom_rule_note
) VALUES
    ('upper-slug', 'claudecode', 'project', 'contains', 'upper', 'unlisted',
     10, 1, '2025-01-01T00:00:00Z', '2025-01-02T00:00:00Z', 'upper-rule-note'),
    ('lower-slug', 'codex', 'project', 'contains', 'lower', 'private',
     20, 1, '2025-02-01T00:00:00Z', '2025-02-02T00:00:00Z', 'lower-rule-note');

INSERT INTO user_github_daily (
    username, day, contributions, commits, synced_at, custom_github_note
) VALUES
    ('Alice', '2025-01-01', 11, 7, '2025-01-02T00:00:00Z', 'upper-github-note'),
    ('alice', '2025-01-01', 22, 9, '2025-01-02T00:00:00Z', 'lower-github-note');
