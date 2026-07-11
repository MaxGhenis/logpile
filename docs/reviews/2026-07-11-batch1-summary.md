# Batch 1 — mechanical safety + launch surface (implemented)

Implements 18 findings from [2026-07-11-sol-full-review.md](2026-07-11-sol-full-review.md).
Implementation: gpt-5.6-sol (edits-only contract). Review, test-staging fix
(B9 attack test collided with the eager private-root claim), and commit:
Claude Fable 5. Full battery at commit time: 240 pytest passed + 9 subtests,
shellcheck clean, eslint clean, production Next build clean.

| Finding | What landed |
|---|---|
| B1 | `serve` defaults to 127.0.0.1; non-loopback binds in private mode require `--unsafe-network`; bind regression tests |
| B2 | `logpile.sh` resolves symlink chains for SCRIPT_DIR, checks uv/bun with install URLs, bootstraps via `uv sync --locked`; README quickstart canonicalized |
| B8 | ENOSPC fails closed (no source-pointing symlinks); `preserve_reviewed_artifact` rejects symlinks via lstat; atomic replace of lexical destination only |
| B9 | Private transitions archive the last surviving transcript into `.{shared}-private` (0700, lstat-walked, O_NOFOLLOW) instead of unlinking; quarantine + rollback paths tested, incl. symlink/non-directory attacks |
| B10 | Late `logpile:private` markers return a structured `PrivateSessionMarker`, tighten the existing row, and remove the stale shared artifact |
| H7 | Singleton user adoption removed from normal sync; explicit migration path; sequential-user tests |
| H9 | Network-share WAL advice removed; sync lock distinguishes contention from unsupported-locking errors |
| H10 | Next 16.2.3 → 16.2.10; bun audit refreshed; production build green |
| H11 | Per-file OSError isolation across the stat/hash/parse/copy window; rotation mid-sync continues instead of aborting |
| H13 | deploy_landing.sh: `--fail-with-body`, nonzero exit on `success:false`, `CLOUDFLARE_ACCOUNT_ID`/`CLOUDFLARE_ZONE_ID` overrides, EXIT trap, post-deploy sha256 verification against site/index.html |
| M6 | Runtime roots 0700; DB/WAL/SHM/lock/staging/copied transcripts 0600; umask-022 mode tests |
| M9 | `_load_jsonl` keeps dicts only and reports structured `JsonlLoadStats`; per-record exceptions contained |
| M10 | Cloudflare credentials move from argv to a 0600 curl `--config` file with newline validation |
| M11+H12 | `logpile db-backup` via `VACUUM INTO` + `quick_check`; Codex SQLite cloud backup snapshot-consistent the same way; README drops commit-the-live-DB advice |
| L1 | Day bucketing parses ISO-8601 and converts to UTC; malformed timestamps rejected |
| L2 | `tokens_per_day` divides boundary months by included days |
| L4 | Lock-contended sync exits with a distinct code instead of silent success |
| L5 | Landing counts `DISTINCT repo_name` labeled "repos"; page regenerated |

Deferred to batch 2 (accounting core): B3, B4, H3, H4, H5, M1, M2, M3.
Deferred to batch 3 (publish/visibility/scale): B5, B6 (scoped), B7, H1 (bug-fix
scope), H2, H6, H8, M4, M5 (documentation), M7, M8 (scoped).
B11/L3 were handled directly in 78cc505; history retraction decision is Max's.
