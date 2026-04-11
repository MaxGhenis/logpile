# Logpile

**Searchable Claude Code and Codex session logs.**

Logpile syncs local agent session JSONL files into a shared directory, indexes them in SQLite, and serves a web UI for browsing transcripts, tracking usage, and publishing per-user profiles like `/u/maxghenis`.

## Quick start

```bash
# New checkout
cd ~/logpile
uv venv --python 3.11 && uv pip install -e .

# Index your sessions
logpile sync

# Start the web UI
logpile serve
# open http://127.0.0.1:5000
```

Logpile defaults to `~/logpile/`. If you still have a legacy `~/agentus/` checkout or `agentus.db`, those are still detected automatically.

## Commands

### `logpile sync`

Copies sessions into `shared/<username>/` and updates the SQLite index.

```text
Options:
  --shared PATH     Shared directory
  --db PATH         SQLite database
  --username TEXT   Override system username
  --machine TEXT    Override hostname
  -v, --verbose     Print each file processed
```

### `logpile serve`

Starts the Flask viewer.

```text
Options:
  --shared PATH     Shared directory
  --db PATH         SQLite database
  --host TEXT       Bind address      [default: 127.0.0.1]
  --port INTEGER    Port              [default: 5000]
  --public          Public read-only mode
```

`--public` enforces the hosted/public contract:
- only `public` profiles and `public` sessions appear in global listings and aggregate APIs
- `unlisted` profiles and sessions stay direct-link only
- `private` sessions are hidden entirely

### `logpile private <session-id>`

Marks a session private. It is hidden from viewers and excluded from listings, profiles, and aggregate totals.

### `logpile visibility <session-id> <private|unlisted|public>`

Sets session visibility explicitly.

### `logpile redact <session-id> <turn-number>`

Currently marks the whole session private. Per-turn redaction is still planned.

### `logpile users`

Lists canonical user slugs and visibility defaults.

### `logpile user <slug-or-username>`

Updates user metadata and defaults.

```text
Options:
  --display-name TEXT
  --bio TEXT
  --avatar-url TEXT
  --profile-visibility [private|unlisted|public]
  --default-session-visibility [private|unlisted|public]
```

### `logpile rules ...`

Automatic visibility rules let you classify newly synced sessions by exact or fuzzy matching instead of only relying on per-user defaults.

```bash
# List rules
logpile rules list --user maxghenis

# Make all Claude Code demo-project sessions private
logpile rules add maxghenis \
  --source-scope claudecode \
  --field project \
  --mode contains \
  --pattern demo \
  --visibility private

# Fuzzily match first prompts and make them unlisted
logpile rules add maxghenis \
  --source-scope codex \
  --field first_user_message \
  --mode fuzzy \
  --pattern "client work" \
  --threshold 0.70 \
  --visibility unlisted

# Recompute existing non-manual sessions after adding rules
logpile rules apply --user maxghenis

# Preview how one session would be classified
logpile rules test session-123
```

Rules support:
- deterministic modes: `equals`, `contains`, `prefix`, `suffix`, `regex`
- fuzzy mode: `fuzzy` with a configurable `--threshold`
- fields: `project`, `source_path`, `first_user_message`, `model`, `machine`, `username`
- optional `--source-scope` to target `claudecode` or `codex`

Precedence:
- manual per-session visibility wins over everything
- then the first enabled rule by ascending `priority`
- then the user's `default_session_visibility`

## Privacy controls

### Ignore files

Logpile reads both `~/.logpile-ignore` and the legacy `~/.agentus-ignore`.

```text
# ~/.logpile-ignore
*salary*
*password*
-Users-maxghenis-personal-*
```

### Inline markers

Add any of these markers to the first user message to skip a session during sync:

- `# logpile:private`
- `# agentus:private`
- `# ccshare:private`

## Web UI

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Overall activity, tools, errors, recent sessions |
| Sessions | `/sessions` | Searchable session index |
| People | `/u` | Public user directory |
| Profile | `/u/<slug>` | Canonical per-user public page |
| Session detail | `/sessions/<id>` | Transcript viewer |
| Analysis | `/analysis` | Tool and command rollups |
| API | `/api/sessions`, `/api/users`, `/api/users/<slug>`, `/api/users/<slug>/stats`, `/api/users/<slug>/sessions` | Public JSON endpoints |
| Private API | `/api/users/<slug>/rules` | Rule inspection in non-public mode only |

## Data model

- `users.slug` is the canonical public identity used in URLs.
- `sessions.user_slug` links every session to a canonical user.
- Session `visibility` is one of `public`, `unlisted`, or `private`.
- Session `visibility_source` is one of `default`, `rule`, or `manual`.
- User `profile_visibility` controls whether `/u/<slug>` is public, unlisted, or hidden.
- User `default_session_visibility` controls the default visibility for newly synced sessions.
- `session_visibility_rules` stores automatic deterministic and fuzzy sharing rules per user.
- In `--public` mode, public APIs and profile pages only include `public` sessions. `unlisted` sessions remain direct-link only.

## Storage layout

```text
~/logpile/
├── logpile.db
└── shared/
    └── <username>/
        ├── claudecode/
        │   └── <project>/
        │       └── <session>.jsonl
        └── codex/
            └── <project>/
                └── <session>.jsonl
```

Legacy `~/agentus/agentus.db` is still supported and auto-detected.

## Supported inputs

| Tool | Path |
|------|------|
| Claude Code | `~/.claude/projects/**/*.jsonl` |
| Codex | `~/.codex/sessions/**/*.jsonl` |

## Multi-user setup

Point each machine at the same shared folder and DB path:

```bash
logpile sync --shared /Volumes/team-share/logpile/shared \
             --db     /Volumes/team-share/logpile/logpile.db

logpile serve --shared /Volumes/team-share/logpile/shared \
              --db     /Volumes/team-share/logpile/logpile.db \
              --host 0.0.0.0 --public
```

## Compatibility

- The legacy `agentus` CLI alias still works after reinstall.
- Existing `~/agentus/shared` and `~/agentus/agentus.db` can stay in place.
- Existing private markers and ignore files are still honored.
