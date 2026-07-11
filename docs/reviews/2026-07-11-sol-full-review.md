# Logpile full pre-launch review — 2026-07-11

## Executive verdict

**No-go for the Wednesday/Thursday public launch in the current state.** The test suite and production build are green, and the live SQLite database is internally healthy, but several core launch promises fail on realistic data: the documented quickstart does not put `logpile` on `PATH`; the default private viewer is unauthenticated and binds to the LAN; privacy review is bypassable and not tied to immutable bytes; two storage transitions can destroy the only transcript or overwrite the original source; large real sessions are buffered whole; and Codex replay/reset handling materially miscounts the existing corpus. The public repository also already contains identifiable session-derived material. Fix the blocker section, rerun the corpus accounting audit and public-mode adversarial tests, then reassess launch readiness.

Verification performed read-only against the checkout and isolated temporary clones: 176 Python tests plus 2 subtests passed; ESLint, ShellCheck, and a fresh production Next build passed; the landing-page bootstrap served an empty fresh-clone database successfully when `uv` and Bun were already installed; `PRAGMA integrity_check` and `foreign_key_check` returned clean; the deployed `https://logpile.ai` response was byte-identical to `site/index.html`; and Gitleaks found no credential findings in repository history. `bun audit --production`, however, reported 14 advisories, including 7 high-severity advisories in the pinned Next release.

## Blocker findings

### B1. The unauthenticated private viewer binds to every network interface

**Files:** `logpile/cli.py:529-540`, `logpile/cli.py:608-623`, `README.md:18-25`, `site/index.html:122-127`, `web/src/app/api/publish/queue/route.ts:22-37`, `web/src/app/api/publish/review/[id]/route.ts:4-18`

`logpile serve` defaults to `0.0.0.0` even though the quickstart tells the user to open `127.0.0.1`. Private mode has no authentication and includes default-unlisted transcripts plus publish-review APIs containing summaries, findings, IDs, and absolute paths, so the landing-page command exposes the archive to any reachable LAN client. **Fix:** default to `127.0.0.1`; refuse a non-loopback bind in private mode unless the user provides an explicit unsafe-network override or authentication; add a socket-level regression test for the default bind.

### B2. Both documented fresh-install paths are broken without unstated local setup

**Files:** `README.md:12-20`, `README.md:362-366`, `logpile.sh:9-30`

The README creates `.venv` and installs into it, then immediately runs bare `logpile`; an isolated reproduction found `.venv/bin/logpile` but got `command not found`. `logpile.sh` always assumes `uv` exists and additionally assumes Bun for `serve`, despite claiming to bootstrap a fresh clone; the Requirements section incorrectly implies Bun itself is installed by `serve`. The documented `~/bin/logpile` symlink is also broken because `SCRIPT_DIR` uses unresolved `BASH_SOURCE[0]`, treating `~/bin` as the repo; this was reproduced. **Fix:** make one canonical quickstart (`./logpile.sh` or `uv run logpile`), explicitly install/check prerequisites, resolve symlink chains before deriving the root, use the lockfile, and add a clean-`PATH` CI smoke test for direct and symlink invocation.

### B3. Codex replay detection drops live work and counts inherited work on real files

**Files:** `logpile/parsers.py:264-313`, `logpile/parsers.py:871-915`, `logpile/parsers.py:917-968`

The parser assumes that the first two valid `token_count` events sharing a wall-clock second prove a replay, and that a replay cannot cross the next second. Both assumptions are false in the corpus: 231 fresh non-fork files were misclassified, discarding 3,982,240 input tokens, 127,066 output tokens, and 232 live tool calls; another real replay copied 36,480 records across a second boundary, causing roughly 236.7M inherited input tokens to be treated as native. **Fix:** require explicit fork/replay evidence, identify the copied prefix structurally rather than with `timestamp[:19]`, and add minimized real-format fixtures for normal duplicate snapshots and multi-second replay bursts.

### B4. Genuine Codex counter epochs are silently discarded

**Files:** `logpile/parsers.py:879-915`, `tests/test_parsers.py:692-704`

The componentwise global-maximum baseline treats every cumulative-counter reset as duplication. A real long-lived root reached about 8.67B input tokens, reset all counters to zero, and continued for weeks; the parser can report only the larger epoch rather than the sum. The unit test explicitly locks in this undercount by asserting that post-reset usage contributes zero. **Fix:** segment credible all-component resets into billing epochs and sum deltas within each epoch after removing any inherited fork prefix; replace the current reset test with an epoch-summing invariant.

### B5. Whole-file buffering can exhaust memory on sessions already present in the index

**Files:** `logpile/parsers.py:182-196`, `web/src/lib/parsers.ts:70-86`, `logpile/publish.py:209-221`, `logpile/publish.py:267-336`

Sync loads every parsed JSON object into a Python list, Next reads the entire transcript synchronously before splitting/parsing it, and publish review holds the complete bytes plus a decoded string. The live index contains two Codex files over 1 GB, a maximum around 1.53 GB, and more than 100 files over 100 MB; one view/review can therefore allocate several gigabytes and block or crash the process. **Fix:** stream ingestion and scanning, store compact state rather than records, paginate/index transcript turns, and stage reviewed bytes to a bounded 0600 file with a hash rather than retaining them in memory.

### B6. Publish review is not an enforcement gate

**Files:** `logpile/cli.py:667-691`, `logpile/cli.py:909-968`, `logpile/db.py:392-396`, `logpile/db.py:657-688`, `logpile/db.py:813-944`, `logpile/sync.py:1054-1077`

`logpile visibility` can move a private session to unlisted or public without review, and unlisted is directly served by public mode; unlisted/public defaults and rules likewise materialize publishable artifacts without invoking review. This contradicts the documented `private → unlisted → public` review model. The low-level visibility normalizer also defaults invalid values to `public`. **Fix:** require a successful review record and reviewed content hash for every transition out of private (or explicitly narrow the product promise and make unlisted local-only), route publishable state changes through one guarded API, reject invalid values closed, and test commands, rules, defaults, and migrations.

### B7. Approval is not bound to immutable bytes

**Files:** `logpile/sync.py:245-251`, `logpile/sync.py:967-990`, `logpile/sync.py:1015-1077`, `web/src/app/sessions/[id]/page.tsx:34-63`, `logpile/web/app.py:1075-1089`

A controlled reproduction approved clean bytes, appended a secret-like value to the source, and synced again; the row stayed public and the shared artifact was silently replaced with the unreviewed bytes. Public detail also falls back to mutable `source_path` when the shared copy is missing. **Fix:** persist `reviewed_sha256` and an immutable reviewed artifact; source changes must leave the old publication frozen and requeue the new revision (or revoke publication); public mode must serve only a hash-verified artifact under the configured publish root and never follow `source_path` or symlinks.

### B8. The ENOSPC symlink fallback can overwrite the original agent log

**Files:** `logpile/sync.py:144-180`, `logpile/publish.py:138-147`, `logpile/publish.py:339-355`

On disk-full, `_copy_session` can replace a missing shared copy with a symlink to the source. `preserve_reviewed_artifact` resolves that symlink and writes reviewed bytes to the resolved path; a controlled race reproduced the newer source being overwritten by the older reviewed bytes. **Fix:** never use source-pointing symlinks as publish artifacts, fail closed on ENOSPC, reject symlinks with `lstat`, and atomically replace only the lexical shared destination after verifying its parent/root and free space.

### B9. Making a rotated session private deletes its only surviving transcript

**Files:** `logpile/db.py:657-688`, `logpile/sync.py:231-234`, `logpile/sync.py:291-319`, `logpile/sync.py:750-826`

For 7,282 current Claude sessions (about 1.75 GB), the source has rotated away and `shared_path` is the only surviving raw copy. Setting one private unconditionally unlinks the shared artifact and clears the DB path; a temp reproduction left neither source nor shared file. **Fix:** never unlink the last durable copy: atomically move it into a private archive outside the public/shared tree before committing visibility, or refuse the transition until a private destination is available; test rollback and source-missing cases.

### B10. Adding an inline private marker after first sync does not tighten visibility

**Files:** `logpile/parsers.py:582-610`, `logpile/parsers.py:825-831`, `logpile/sync.py:1015-1018`, `logpile/sync.py:1227-1230`

Parsers return `None` when they see `logpile:private`, and the sync loops treat that identically to an unparseable file: they skip it without updating the existing row or removing the prior shared artifact. Reproduction left an indexed session unlisted with its old shared copy after the marker was appended; a previously public row would remain public. **Fix:** return a structured private-marker result and atomically tighten any existing row plus reconcile storage; add transition tests, not only first-ingest tests.

### B11. The public Git history contains identifiable private session-derived material

**Files:** `notes/token-forensics-2026-04-13.md:194-210`, `notes/token-forensics-2026-04-13.md:228-265`

The tracked note contains a real session UUID and identifiable fragments about private correspondence and proposal/research workstreams. This is not a credential-shaped leak, so Gitleaks correctly did not catch it, but Show HN will amplify material already retained in Git history and may help locate an unlisted session later. **Fix:** redact/generalize the note, make the referenced session private, and decide before launch whether confidentiality warrants rewriting the public Git history; removing the current file alone does not retract prior commits.

## High findings

### H1. Fresh sync duplicates the entire archive and its ENOSPC recovery can permanently stale artifacts

**Files:** `logpile/sync.py:99-125`, `logpile/sync.py:144-181`, `logpile/sync.py:203-242`, `logpile/sync.py:1080-1136`, `logpile/sync.py:1293-1347`, `logpile/db.py:520-556`

Every default-unlisted session is copied into `shared/`; on this machine roughly 48 GB of current source roots coexist with a 50 GB shared tree plus a 1.7 GB DB, with no preflight or warning (about 1.75 GB of `shared/` is sole-survivor rotated content rather than duplication). If an existing copy update hits ENOSPC, the old file is preserved but the new source hash/mtime is committed; the next fast path checks only existence and can skip the stale artifact forever. **Fix:** index source files in place and materialize only reviewed/exported content, or make archival copying explicit with size/free-space planning; never advance source metadata unless the shared hash matches, and persist a retry state.

### H2. The scanner misses broad credential and PII classes while the UI claims completeness

**Files:** `logpile/publish.py:66-120`, `logpile/publish.py:209-238`, `web/src/app/publish/review/[id]/page.tsx:171-177`

Crafted checks produced no finding for common fine-grained source-control, messaging, payment, package-registry, cloud, JWT, Basic-auth, SSN, card, phone, and Windows-home examples. A credential-bearing database URL was only classified as a medium email, and metadata such as `git_branch` is not scanned. **Fix:** add maintained provider patterns plus authorization/URI/JWT/entropy checks, context-aware PII and Luhn validation, Windows paths, and all rendered metadata; change the UI to “No configured patterns detected; manual review required” and keep adversarial positive/negative fixtures.

### H3. Codex leaf metadata and parent keys do not represent the stored session graph

**Files:** `logpile/parsers.py:842-856`, `logpile/sync.py:1155-1160`, `logpile/sync.py:1293-1327`, `web/src/lib/db.ts:1484-1501`

All 1,106 inspected multi-`session_meta` files let replayed ancestor metadata overwrite leaf identity/start metadata; deeper replay chains can also replace the immediate parent. Separately, `parent_session_id` stores a raw thread UUID while `sessions.session_id` is the full rollout filename stem: 0 of 3,390 current parent references join exactly, although nearly all match by suffix. Date filters, lineage, and context-explosion analysis are therefore structurally wrong. **Fix:** store explicit `thread_id`/`parent_thread_id`, take identity/date/immediate parent only from the first leaf metadata, resolve canonical parents during sync, and add graph-integrity tests.

### H4. Claude Code subagents are misclassified as root short tasks

**Files:** `logpile/parsers.py:639-640`, `logpile/parsers.py:816-817`, `logpile/stats.py:33-48`

Claude parsing always returns `spawn_depth=0`. The current DB has 7,772 subagent-path/agent rows at depth zero; 7,595 classify as `short-task` rather than `subagent`, representing about 94.3M native output tokens. **Fix:** derive sidechain/agent identity and parentage from `isSidechain`, `agentId`, root `sessionId`, and the `/subagents/` path, then backfill and regression-test `stats.py` classification on real-format fixtures.

### H5. Date-bounded stats mix cohort and event-period semantics

**Files:** `logpile/stats.py:62-85`, `logpile/stats.py:95-143`, `logpile/stats.py:226-258`, `logpile/stats.py:295-346`

Overview/pattern/repo totals select sessions by `first_timestamp` and include their entire lifetime, while monthly totals select usage by event day. A session started before the range but active inside it is excluded from the overview and included by month; one started inside contributes post-range tokens to the overview. A July comparison differed by about 4.1B input and 14.7M output tokens. **Fix:** when bounds are supplied, derive all token totals and active-session counts from `session_daily_effective`, filter tool rows by tool timestamp, and document one consistent period semantic.

### H6. Public context-explosion analysis crosses visibility boundaries

**Files:** `web/src/lib/db.ts:1471-1541`, `web/src/lib/db.ts:1545-1565`, `web/src/app/analysis/page.tsx:198-237`, `web/src/app/analysis/page.tsx:285-289`, `logpile/web/app.py:1221-1289`

Only leaf rows receive `listed_public`; recursive lineage walks raw `sessions`, and the final root join has no visibility condition. A synthetic public-mode reproduction returned 404 for a private root detail page but rendered that root's private goal/summary sentinels on `/analysis` through two public children. **Fix:** apply visibility at every `EXISTS`, recursive hop, and root join (or stop at the first invisible node), and add mixed-visibility lineage tests to both Next and legacy Flask implementations.

### H7. The advertised multi-user flow merges later users into the first account

**Files:** `logpile/cli.py:224-234`, `logpile/sync.py:128-141`, `README.md:343-355`

Both username resolvers adopt the sole existing user; the internal resolver does so even after an explicit new username reaches sync. A reproduction with Alice already in the DB attributed Bob to Alice, so the documented multi-machine workflow silently corrupts ownership and storage paths. **Fix:** remove singleton adoption from normal sync, expose legacy adoption as an explicit migration, and test sequential distinct users and explicit `--username`.

### H8. `logpile backup` omits durable transcripts that sync indexes

**Files:** `logpile/backup.py:317-344`, `logpile/sync.py:80-96`, `logpile/sync.py:750-826`

Backup omits `.codex-2`, `.codex-3`, OpenClaw roots, and every DB-referenced shared artifact whose source rotated away. A backup taken during review would miss 7,282 sole-survivor Claude files (about 1.75 GB) plus 41 indexed alternate-root sessions. **Fix:** centralize discovery between sync and backup, accept the DB/shared roots, enumerate every durable transcript path, deduplicate by full SHA-256, and test all roots plus rotated-only rows.

### H9. The README recommends WAL SQLite on a network share

**Files:** `README.md:343-358`, `logpile/db.py:1583-1588`, `logpile/sync.py:845-855`

The multi-user instructions place one WAL database on `/Volumes/team-share`, but SQLite WAL relies on same-host shared memory and is not supported across network filesystems. The sync lock also treats every `flock` error, including unsupported locking, as ordinary contention and exits successfully. **Fix:** remove the multi-host shared-SQLite claim; use one DB-owning service/host, Postgres, or independent local DBs, and distinguish `EAGAIN`/`EACCES` from lock-system errors. See the [SQLite WAL constraints](https://sqlite.org/wal.html).

### H10. The pinned Next release has known high-severity production advisories

**Files:** `web/package.json:11-28`, `web/bun.lock:9-22`

`bun audit --production` reported 14 advisories, including 7 high-severity advisories against Next 16.2.3 (server-component/connection-exhaustion DoS, SSRF, and routing/proxy issues among them). Not every advisory is reachable in this app, but the unauthenticated network exposure makes accepting the set unjustified. The npm registry reported 16.2.10 as current during review; the audit's fixed floor is 16.2.5. **Fix:** update and lock a verified patched release, refresh transitive PostCSS, rerun production audit/build/public-mode tests, and record the resolved versions.

### H11. Normal archive rotation can abort an entire sync

**Files:** `logpile/sync.py:80-96`, `logpile/sync.py:971-980`, `logpile/sync.py:1185-1192`, `logpile/parsers.py:1004-1010`

Sync catches `stat()` failure but hashes, parses, and copies through later path reopens outside that guard. A live-to-archive rename in that window raises `FileNotFoundError` and aborts the run instead of letting the archived-root pass pick it up. **Fix:** process a stable open descriptor/snapshot and `fstat` it, or isolate per-file `OSError` with a retry/continue path; add a forced rename-between-stat-and-hash test.

### H12. The Codex SQLite cloud backup is not a point-in-time snapshot

**Files:** `logpile/backup.py:277-309`, `logpile/backup.py:327-330`, `logpile/backup.py:1201-1231`

The active `logs_2.sqlite` (about 1.3 GB here) and its WAL/SHM are copied independently. A write/checkpoint between those copies can produce an inconsistent restore set. **Fix:** use SQLite's online backup API or `VACUUM INTO` to create one temporary consistent database, upload that artifact rather than independent WAL/SHM files, and verify it with `quick_check` before upload.

### H13. Landing deployment can report success after a failed Cloudflare API call

**Files:** `scripts/deploy_landing.sh:5-22`, `scripts/deploy_landing.sh:34-46`

`curl -s` does not fail on HTTP errors, both response parsers print Cloudflare errors but exit zero when `success:false`, and the script always prints `deployed`; it also chooses `accounts[0]`. A launch-day update can therefore silently remain undeployed. **Fix:** configure explicit account/zone IDs, use `curl --fail-with-body`, make response validation exit nonzero, verify the deployed body/hash, and install an `EXIT` trap for the temporary worker file.

## Medium findings

### M1. Claude cache-write subtype totals can exceed total cache writes

**Files:** `logpile/parsers.py:691-699`

Fallback records can combine final top-level totals with a stale first-attempt `cache_creation` breakdown. Eleven current sessions violate `5m + 1h == cache_creation` by a combined 2,010,474 tokens; matching final values existed in `usage.iterations` for most inspected mismatches. **Fix:** select the iteration whose usage tuple matches the top-level result, enforce the split invariant, and record an explicit unknown remainder rather than accepting contradictory totals.

### M2. A partial daily row suppresses fallback for all unattributed usage

**Files:** `logpile/parsers.py:24-43`, `logpile/parsers.py:244-257`, `logpile/db.py:296-356`, `logpile/db.py:1761-1794`

If a timestamped message creates a daily row while an assistant usage record lacks a timestamp, session totals include the tokens but daily totals do not; the existence of any row disables whole-session fallback. Claims with `day=NULL` have the same native-daily gap. Current resynced rows reconcile, so this is a malformed/partial-record edge rather than observed drift. **Fix:** persist an explicit approximated residual day (or documented nearest-event attribution) and enforce that daily component sums equal the session row.

### M3. The winner-only message-claims table cannot promote an unchanged losing claimant

**Files:** `logpile/db.py:1424-1429`, `logpile/db.py:1813-1931`

Only the current owner is stored. If that owner reparses without a key, disappears, or changes rank, the claim can be deleted or remain misowned; an unchanged duplicate transcript is not considered until it happens to reparse. Raw inspection also found claimant copies with materially different usage, so they are not safely interchangeable. **Fix:** store occurrences keyed by `(claim_key, session_id)`, derive the minimum-ranked owner from all current claimants, and recompute on claim or rank changes.

### M4. Scanner evidence can reveal a credential yet omit the actual match location

**Files:** `logpile/publish.py:133-135`, `logpile/publish.py:209-221`, `logpile/publish.py:627-635`

Evidence is the first 180 characters of a JSONL line, not a match-centered excerpt, and only one hit per rule per line is emitted. A match around byte 500 triggered a finding whose evidence did not show it; a match near the start is printed verbatim into CLI/browser output. **Fix:** use `finditer`, store offsets/counts, center excerpts on matches, and mask the credential itself before any serialization or logging.

### M5. The landing page publishes activity from zero published sessions

**Files:** `scripts/build_landing.py:25-40`, `site/index.html:127-136`

The generator aggregates all sessions/daily rows without a visibility join. During review every indexed session was unlisted and `listed_public=0`, yet the public page disclosed total volume and exact daily session/token cadence next to “Nothing leaves your machine unless you publish.” The aggregate publication may be intentional, but it is not represented in the visibility model or trust copy. **Fix:** either filter through `listed_public=1` or add a separate explicit aggregate-publication consent/configuration and test that private/unlisted activity cannot enter the page accidentally.

### M6. Local data permissions are not hardened

**Files:** `logpile/db.py:1582-1598`, `logpile/sync.py:144-162`

The DB, WAL, and SHM are 0644; Codex originals and copied Codex transcripts preserve 0644 under 0755 directories. Other local OS users can read prompts, paths, commands, and raw transcripts despite the local-first privacy positioning. **Fix:** create runtime roots at 0700 and DB/WAL/SHM/lock/staging/transcript files at 0600 by default, with an explicit sharing opt-out and mode tests under umask 022.

### M7. Legacy identity migration can fail or lose columns

**Files:** `logpile/db.py:986-1065`, `logpile/db.py:1160-1328`, `logpile/db.py:1390-1423`

Legacy usernames are normalized into a new primary key without collision handling; a fixture containing case variants failed permanently with `UNIQUE constraint failed`. The rebuild's fixed session/user column lists also omit newer fields (for example `github_username`, which is added before the rebuild), and newer token/stat columns are re-added as zeros after replacement. **Fix:** introduce explicit schema versions and transactional migration fixtures for every released schema, deterministically resolve identity collisions, enumerate/preserve all columns, and snapshot the DB before destructive rebuilds.

### M8. A built Python wheel cannot serve either bundled web UI

**Files:** `pyproject.toml:22-28`, `logpile/cli.py:566-575`, `logpile/web/app.py:23`

An isolated `uv build` succeeded, but the wheel contained Python modules only: neither the top-level Next app nor Flask templates/static files were packaged. A non-editable install therefore cannot run default `serve`, and the fallback lacks its templates. **Fix:** either state and enforce “source checkout only,” or package versioned web assets with explicit package-data/manifests and install-wheel smoke tests; update the deprecated license-table metadata while touching packaging.

### M9. One valid non-object JSON value can crash the whole parser

**Files:** `logpile/parsers.py:182-211`

`_load_jsonl` accepts any valid JSON value, but downstream code immediately calls `.get`; a line containing `null`, a string, or an array raises rather than being isolated. A 600-file real-data sample did not contain such a record, so this is hardening rather than a current corpus failure. **Fix:** retain only dictionaries, count/report malformed record types, and contain unexpected per-record exceptions without aborting the full sync.

### M10. Cloudflare deployment uses an overprivileged secret in process arguments

**Files:** `scripts/deploy_landing.sh:5-17`, `scripts/deploy_landing.sh:21-43`

The script uses the legacy Global API Key (all resources for the user) and puts it in each curl command line, where other local processes may observe it. `agent-secret` retrieval is appropriate, but the credential scope and transport are not. **Fix:** use a token scoped to the one account/zone and required Worker/domain permissions, and pass headers through a 0600 curl config or stdin rather than argv.

### M11. The suggested Git backup is ignored by default and is not a safe SQLite snapshot

**Files:** `README.md:343-358`, `.gitignore:14-17`, `logpile/db.py:1583-1588`

The README suggests committing `shared/` and `logpile.db`, but both paths are ignored. Even if force-added, committing only the main DB while WAL is active can omit committed pages still in the WAL. Cloud backup also excludes `logpile.db`, so profiles, rules, review decisions, visibility, and other non-reconstructible metadata have no supported backup path. **Fix:** provide a `logpile db-backup` command using SQLite's online backup API or `VACUUM INTO`, document metadata and transcript coverage explicitly, and remove the direct-live-DB Git advice.

## Low findings

### L1. “UTC day” bucketing only slices the timestamp text

**Files:** `logpile/parsers.py:244-257`

An offset timestamp just after local midnight can belong to the previous UTC day, but `_day_of` returns its first ten characters. The current corpus uses `Z`, so no observed rows are affected. **Fix:** parse ISO-8601, convert to UTC, then take the date; reject malformed timestamps.

### L2. `tokens_per_day` misstates bounded partial-month rates

**Files:** `logpile/stats.py:329-374`

A bounded range longer than 30 days can include only part of its first/last months, but those boundary-month totals are divided by the full calendar month (or all days elapsed in the current month). **Fix:** divide by distinct included days or by the intersection of the requested range and calendar month.

### L3. Generated Supabase connection metadata is tracked

**Files:** `supabase/.temp/pooler-url:1`, `supabase/.temp/project-ref:1`, `.gitignore:1-37`

The files disclose the live project reference and pooler endpoint; no password is present. **Fix:** remove generated `supabase/.temp/*` from the index/history as appropriate and ignore `/supabase/.temp/`.

### L4. A lock-contended sync exits as a normal successful sync

**Files:** `logpile/sync.py:845-855`, `logpile/cli.py:493-501`

The locked path returns `(0, 0, 0)`, after which the CLI prints an ordinary success summary and exits zero; schedulers cannot distinguish “nothing changed” from “no sync ran.” **Fix:** return a typed locked result or a distinct nonzero/retryable exit code.

### L5. The landing page labels distinct repo names as “repo checkouts”

**Files:** `scripts/build_landing.py:25-40`, `scripts/build_landing.py:73-76`, `site/index.html:128`

The query counts `DISTINCT repo_name`, which collapses multiple checkouts/worktrees of the same repository, while the page calls the result “repo checkouts.” **Fix:** change the label to “repos” or count a checkout-level key such as normalized `repo_root`.

## Solid areas that should not be churned

- The live 1.7 GB SQLite database passed `integrity_check`; current rows with daily usage reconcile exactly to session totals, native totals, and assistant counts, and `total_input = fresh + cache-read + cache-write` holds.
- Claude cache writes are correctly included in total input, and Codex cached input is correctly treated as a subset of `input_tokens` rather than added twice.
- The native refresh path is idempotent and the `native_refresh_pending` flag provides sensible interrupted-sync recovery; retain that pattern while replacing winner-only claim storage.
- Normal shared-copy writes use a temp file plus `os.replace`, so readers do not observe partial files; keep the atomic-copy pattern and remove only the symlink fallback.
- The same-host sync lock prevents ordinary overlapping sync writers.
- The centralized `listed_*`/`direct_*` clauses protect the remaining audited Next list/profile/API queries, and publish APIs also return 404 in public mode. Preserve that centralization and add lineage-specific coverage.
- SQL values are parameterized or allowlisted; the publish bridge uses `execFile` rather than a shell.
- Transcript rendering uses escaped React text/`JSON.stringify`, with no `dangerouslySetInnerHTML`; legacy Jinja is autoescaped. No transcript XSS path was found.
- Gitleaks found no credential-shaped secret in current Git history. The tracked privacy issue is semantic/human material, not a failed credential regex.
- The checked-in landing page exactly matches the live Cloudflare response and contains no transcript text, indexed session usernames, repo names, absolute paths, or credentials beyond the separately noted aggregate cadence and Supabase metadata in the repo.
