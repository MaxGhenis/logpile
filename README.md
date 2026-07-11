# Logpile

**Your Claude Code and Codex sessions, indexed and legible.**

Logpile turns the JSONL session files your agent CLIs leave on disk into something you can actually browse — filtered by repo, activity (wrote files? ran tests? committed?), status (success / partial / failed / exploration), and workflow origin (your direct work vs. delegated agents vs. pipeline runs). A publish queue reviews each session for secrets and PII before anything goes on a public profile.

Site: **[logpile.ai](https://logpile.ai)**
Source: **[github.com/MaxGhenis/logpile](https://github.com/MaxGhenis/logpile)**

---

## Quick start

```bash
git clone https://github.com/MaxGhenis/logpile ~/logpile && cd ~/logpile
uv venv --python 3.11 && uv pip install -e .

logpile sync          # index your local CC and Codex sessions
logpile search "thing I asked about"
logpile serve         # open http://127.0.0.1:5002
```

`logpile serve` builds and starts the Next.js app. Add `--dev` for HMR during development. Add `--public` to expose only public sessions and profiles (safe for hosted instances).

Logpile is local-first by default. Nothing leaves your machine unless you opt into the cloud backend with `LOGPILE_BACKEND=cloud`, `--backend cloud`, or the `logpile backup` commands.

---

## What Logpile gives you

### Session archive with real filters
Not just a JSONL viewer — filter by repo, git branch, activity (wrote files, ran tests, built, committed), session status, and workflow origin.

### Workflow lens
Every analytics page splits by *how the session started*: your direct work, delegated sub-agents, pipeline evals, meta-scaffolding, or system-generated. Stop mixing "what I coded this week" with what your test runners did overnight.

### Publish queue with secret/PII scanning
Before anything leaves `private` → `unlisted` → `public`, Logpile scans the transcript and flags findings by severity. You get a recommended visibility and a list of evidence, per session, so the triage step is actual review, not guesswork.

### Context explosion analysis
For Codex: detects fork-swarm roots whose children are carrying large inherited-context burden. Surfaces cached-input share, child-token share, spawn depth, and warnings like "mostly inherited context", "fork swarm", "giant child sessions". Tells you where your token budget is actually going.

### Operator profiles at `/u/<username>`
Public-by-default pages showing session counts, activity stats (files written, test runs, builds, commits), top repos and tools, and a curated recent-sessions feed with status badges and summaries.

---

## Architecture

```
┌─ Python CLI (logpile)            ┌─ Next.js 16 app (web/)
│  - sync (JSONL → SQLite/R2)      │  - dashboard, sessions, profiles
│  - publish queue / review        │  - reads SQLite via better-sqlite3
│  - visibility rules engine       │  - Tailwind 4, Recharts
└────┬──────────────────────────┐  └───┬──────────────────────────┐
     │                          │      │                          │
     ▼                          ▼      │                          ▼
   sessions table       shared/*.jsonl │                     browser UI
     (SQLite)          (local cache)   │
                                       ▼
                            /api/publish/review/[id]
                            (shells to Python publish.py
                             for secret/PII scanning)
```

**Why split like this?** The parsing and scanning logic is Python (one source of truth, covered by the CLI test suite). The UI is TypeScript (DX you'd actually want for a read-heavy app). The catalog views in SQLite are the contract between them, and the one awkward boundary (review file scanning) shells to Python on demand.

---

## Commands

### `logpile sync`

Indexes your local session JSONL files into SQLite by default. Use `--backend cloud` to upload immutable raw logs to R2/S3 and index exact chunks in Supabase/Postgres, or `--backend both` to do both.

```
Options:
  --shared PATH     Shared directory    [default: ~/logpile/shared]
  --db PATH         SQLite database     [default: ~/logpile/logpile.db]
  --backend MODE    local | cloud | both [default: local]
  --username TEXT   Override system username
  --machine TEXT    Override hostname
  -v, --verbose     Print each file processed
```

Scans `~/.claude/projects/**/*.jsonl` plus every Codex rollout root —
`~/.codex/sessions`, `~/.codex/archived_sessions`, `~/.codex-2/sessions`,
`~/.codex-3/sessions`, and OpenClaw codex homes
(`~/.openclaw/agents/*/agent/codex-home/sessions`) — extracts repo metadata,
activity counts, narrative fields, and origin classification, then writes to
SQLite. If a rollout stem exists in more than one root (mid-archive race),
the live `sessions/` copy wins. Unchanged files are skipped on a size+mtime
fast path, so multi-GB immutable archives are hashed once, not every sync.

#### Token accounting

- **Codex** `token_count` events carry *cumulative* counters, and resuming or
  forking a session writes a new rollout file that replays the whole prior
  history re-stamped into a single wall-clock second under a fresh session
  id. Sync detects that leading same-second burst (two `token_count` events
  can never share a second live), folds it into a delta baseline, and
  accumulates clamped per-event deltas — so each file contributes only its
  live continuation, and replayed messages/tool calls are not re-counted
  either. (`first_user_message` is still taken from the replay so resumed
  sessions keep their topic.)
- **Claude Code** assistant records are deduplicated by `message.id` within a
  file, and cache writes (`cache_creation_input_tokens`, split 5m/1h) are
  captured alongside fresh input and cache reads.
  `total_input_tokens = fresh + cache_creation + cache_read`.
- **Per-day usage** (`session_daily_usage`) buckets tokens, messages, and
  tool calls by the UTC day of the underlying events. Date-bucketed rollups
  (`logpile stats` by-month, the per-day charts in both web UIs) read the
  `session_daily_effective` view, which falls back to start-date attribution
  for sessions not yet re-synced — a session spanning weeks no longer dumps
  all its usage on its start date.

##### Cross-session dedup (`native_*` columns)

Resuming a *Claude Code* session copies prior history into a new file and
re-stamps each record's `sessionId`, so replayed Claude messages are locally
indistinguishable from native ones — but they keep their original
`message.id`, `requestId`, `uuid`, and timestamps. Sync claims each parsed
assistant message in the `message_claims` table under the same key the
usage-tracker pipeline uses (`message.id:requestId`); among the sessions
containing a message, the owner is the one with the smallest
`(last_timestamp, first_timestamp, session_id)` — the earliest-ending
transcript, i.e. the session where the message actually ran (a resume copy
always ends at or after its source). That min-rule is order-independent, so
re-parsing files in any order, or rebuilding the database from scratch,
converges to the same owners.

Every token column therefore exists in two flavors:

- `total_input_tokens` etc. — **transcript semantics**: what this session's
  file contains, including history inherited through a resume. Correct for
  "how big was this session's context", double-counts across a resume chain.
- `native_total_input_tokens` etc. (plus `native_assistant_message_count`) —
  **native semantics**: usage first attributed to this session. Summing
  native columns across sessions never double-counts. For Codex, parse-time
  replay handling already makes transcript totals live-only, so native
  mirrors them; for pre-claims Claude rows whose bytes are gone everywhere,
  native falls back to mirroring transcript totals.

Aggregates (`logpile stats`, dashboard totals, per-day charts, per-user and
per-repo rollups) read `native_*`. Per-session rows in the UI still show
transcript totals. `user_message_count` and `tool_call_count` remain
transcript-level everywhere (only assistant records carry the identity
needed for claims).

### `logpile search` / `logpile show` / `logpile status`

Read from either local SQLite/shared files or the cloud raw-log index.

```bash
# Auto chooses cloud when LOGPILE_SUPABASE_DB_URL is set, otherwise local.
logpile search "specific thing x"
logpile show <session-id>
logpile status

# Force one backend.
logpile search "specific thing x" --backend local
LOGPILE_SUPABASE_DB_URL="postgresql://..." logpile search "specific thing x" --backend cloud
```

Local mode is the privacy-preserving path: it reads only your local SQLite database and local JSONL files. Cloud mode reads Supabase/Postgres search chunks and links those chunks back to immutable raw objects in R2/S3.

### `logpile backup`

Backs up immutable raw logs to object storage and indexes exact searchable chunks in Supabase/Postgres. This is not a summarization path: message text, tool inputs, and tool outputs are chunked from the original JSONL so searches can link back to exact raw records.

```bash
# See the exact local files and bytes that would be preserved.
logpile backup plan

# Print the Postgres schema for Supabase.
logpile backup schema

# Upload raw objects to R2/S3 and index exact chunks in Supabase Postgres.
LOGPILE_SUPABASE_DB_URL="postgresql://..." \
LOGPILE_R2_ACCOUNT_ID="..." \
LOGPILE_R2_BUCKET="logpile-raw" \
LOGPILE_R2_ACCESS_KEY_ID="..." \
LOGPILE_R2_SECRET_ACCESS_KEY="..." \
logpile backup push

# Rehydrate a new database from local originals without duplicating sha256s already indexed.
logpile backup push --missing --defer-search-index

# Search the exact raw chunks, not summaries.
logpile backup search "specific thing x"

# Build/rebuild searchable chunks from raw objects already in R2/S3.
logpile backup index --missing

# Rebuild from content-addressed R2/S3 objects when the Postgres manifest is absent.
logpile backup index --from-r2 --missing

# For large imports, defer and build the full-text search index once afterward.
logpile backup index --from-r2 --missing --defer-search-index
logpile backup search-index
```

Install cloud support with `uv pip install -e '.[cloud]'`. Raw objects are stored under content-addressed keys such as `raw/sha256/ab/<sha256>.jsonl`; Postgres stores object manifests, source paths, byte ranges, and exact searchable text chunks. Full-text search is indexed by default, while substring search falls back to a direct scan so bulk imports do not create a very large trigram index unless a deployment chooses to add one later. The backup command never deletes local files.

### `logpile serve`

Starts the Next.js web app.

```
Options:
  --shared PATH     Shared directory
  --db PATH         SQLite database
  --host TEXT       Bind address        [default: 0.0.0.0]
  --port INTEGER    Port                [default: 5002]
  --public          Public read-only mode
  --dev             Next.js dev server with HMR
```

`--public` mode enforces the hosted contract:
- only `public` sessions appear in listings and aggregate APIs
- `unlisted` sessions remain direct-link only
- `private` sessions are hidden entirely
- `/publish` queue is unavailable

### `logpile publish queue`

Prints the publish review queue as JSON — pending sessions plus review findings.

```
Options:
  --visibility [pending|all|private|unlisted|public|needs_changes]
  --status     [exploration|success|partial|failed]
  --origin     [human_direct|human_delegated|pipeline_eval|meta_scaffolding|system_generated]
  --user       <slug-or-username>
  --limit      <int, 1–200>
  --reviews / --no-reviews
  --json
```

### `logpile publish review <session-id>`

Scans one session's transcript for secrets/PII and prints a recommendation + findings as JSON.

### `logpile publish approve <session-id>` / `logpile publish apply <session-id>`

Apply a reviewed visibility decision. Defaults to `--visibility unlisted`. `--force` overrides the review recommendation.

### `logpile private <session-id>` / `logpile visibility <session-id> <level>`

Set session visibility manually. Manual settings win over rules.

### `logpile users` / `logpile user <username>`

Inspect or update user metadata (display name, bio, avatar, profile visibility, default session visibility).

### `logpile rules <add|list|apply|test|delete>`

Automatic session visibility rules per user. See the [rules reference](#visibility-rules) below.

---

## Privacy model

Three session visibility levels:

| Level | In listings | Direct-link | Profile totals | Public mode |
|---|---|---|---|---|
| `public` | ✓ | ✓ | ✓ | ✓ visible |
| `unlisted` | hidden | ✓ | ✓ | direct-link only |
| `private` | hidden | hidden | — | hidden entirely |

Plus matching profile visibility at `/u/<username>`.

### Three layers of control

1. **Manual** — `logpile private <id>` / `logpile visibility <id> <level>`. Always wins.
2. **Rules** — per-user patterns matched by `project`, `source_path`, `first_user_message`, `model`, `machine`, or `username`. See below.
3. **Default** — user's `default_session_visibility`, applied to any session without a manual setting or matching rule.

### Visibility rules

```bash
# Make all CC demo-project sessions private
logpile rules add maxghenis \
  --source-scope claudecode --field project --mode contains --pattern demo \
  --visibility private

# Fuzzily match first prompts and mark them unlisted
logpile rules add maxghenis \
  --source-scope codex --field first_user_message --mode fuzzy \
  --pattern "client work" --threshold 0.70 --visibility unlisted

# Recompute all non-manual sessions with current rules
logpile rules apply --user maxghenis
```

Rules support modes `equals`, `contains`, `prefix`, `suffix`, `regex`, `fuzzy` (with `--threshold`). Precedence: manual > first enabled rule by priority > user default.

### Ignore files and inline markers

`~/.logpile-ignore` (and legacy `~/.agentus-ignore`) skip files at sync time, gitignore-style:

```
*salary*
*password*
-Users-maxghenis-personal-*
```

Or add `# logpile:private` (or `# agentus:private`, `# ccshare:private`) anywhere in the first user message of a session to mark it private at sync time.

### Publish queue

Before publishing anything, `/publish` shows every session needing review with:
- current visibility + status badges
- narrative summary
- recommended visibility from the scanner
- finding count (high / medium severity)
- categories: secrets, PII, structural issues

One-click to `/publish/review/<id>` for full findings with evidence and line numbers.

---

## Web UI pages

| Route | Description |
|---|---|
| `/` | Dashboard — stats, activity chart, CC vs Codex, top tools, errors, recent sessions |
| `/sessions` | Session archive with search, repo/branch/activity/status/origin filters |
| `/sessions/<id>` | Session detail: narrative header + full transcript |
| `/u` | People directory |
| `/u/<username>` | Operator profile — stats, activity, top repos, model mix, recent sessions |
| `/repos` | Repo index |
| `/analysis` | Operator stats, top tools, bash commands, shared utilities, context explosion, runaway sessions, objective relaunches |
| `/publish` | Private publish queue with review findings (hidden in `--public` mode) |

### Public JSON APIs

`/api/sessions`, `/api/users`, `/api/users/<username>`, `/api/users/<username>/stats`, `/api/users/<username>/sessions`. Same visibility rules as the UI.

---

## Multi-user setup

Point each machine at the same shared folder and DB path:

```bash
# On each team member's machine:
logpile sync --shared /Volumes/team-share/logpile/shared \
             --db     /Volumes/team-share/logpile/logpile.db

# One person runs the public viewer:
logpile serve --shared /Volumes/team-share/logpile/shared \
              --db     /Volumes/team-share/logpile/logpile.db \
              --host 0.0.0.0 --public
```

Or commit the shared directory + SQLite to a private git repo. The DB uses WAL so concurrent readers work fine while someone else is syncing.

---

## Requirements

- Python 3.11+ (for the sync CLI and publish scanner)
- [bun](https://bun.sh) ≥ 1.3 (for the Next.js app — installed automatically on first `logpile serve`)
- Everything else is local. No external services, no accounts required.

## Legacy compatibility

The project was previously named `ccshare` then `agentus`. Legacy is preserved:

- `agentus` CLI alias still works after reinstall
- `~/agentus/agentus.db` auto-detected if `~/logpile/logpile.db` doesn't exist
- `.agentus-ignore` files still honored
- `# agentus:private` / `# ccshare:private` markers still skip sessions

---

## License

MIT.
