# Batch 2 — accounting core (implemented)

Implements B3, B4, H3, H4, H5, M1, M2, and M3 from
[2026-07-11-sol-full-review.md](2026-07-11-sol-full-review.md). Changes are
left uncommitted in the working tree for reviewer commit. No Git write command
or full-corpus resync was run.

Minimized real-format JSONL fixtures were added first under
`tests/fixtures/codex/` and `tests/fixtures/claudecode/`. They cover a normal
Codex duplicate snapshot, a multi-second copied prefix, a fresh same-second
counter sequence, an explicit counter reset, canonical parent/child rollouts,
Claude root/sidechain/workflow shapes, fallback usage iterations, an unknown
cache remainder, timestamp-less usage, resume claims, and a period-spanning
session.

## Findings

| Finding | Files touched | What changed | Test evidence |
|---|---|---|---|
| B3 | `logpile/parsers.py`, `logpile/sync.py`, `tests/test_parsers.py`, `tests/test_sync.py`, `tests/fixtures/codex/*`, `README.md` | Removed the same-second replay heuristic. A copied prefix now requires adjacent leaf/ancestor `session_meta` records whose IDs match `forked_from_id`; the first `task_started` whose preserved `started_at` agrees with its own outer clock begins native work. Fresh files keep same-second work, while copied prefixes may span any number of seconds. Token version 7 and expanded shared-copy backfill recover previously suppressed messages, tools, daily rows, and tokens on resync. Rate-limit-only token events cannot masquerade as zero resets. | Fixture tests assert normal and multi-second replay suppression, fresh same-second preservation, live tools/messages, and rotated shared-copy recovery. `tests/test_parsers.py`: 41 passed; `tests/test_sync.py`: 65 passed + 7 subtests. |
| B4 | `logpile/parsers.py`, `tests/test_parsers.py`, `tests/fixtures/codex/counter-reset-epochs.jsonl`, `README.md` | Codex counters are folded as per-epoch maxima. An explicit all-zero cumulative vector ends the current billing epoch; later deltas count from zero. Replay folding keeps only the terminal inherited epoch baseline. Small all-component telemetry wobbles remain clamped and do not create epochs. | The old reset-discards-usage test was replaced by epoch-summing and daily/session reconciliation invariants, plus replay→native→reset and wobble regressions. The real reset spot check adds 8,666,721,598 input and 15,160,072 output tokens versus the old logic. |
| H3 | `logpile/parsers.py`, `logpile/db.py`, `logpile/sync.py`, `tests/test_parsers.py`, `tests/test_sync.py`, Codex parent/child fixtures | Added persisted `thread_id`, `parent_thread_id`, and `identity_version`. Codex identity, start date, immediate parent, and depth come only from the first leaf `session_meta`; replayed ancestors cannot overwrite them. Sync resolves raw parents after all live and rotated files are processed, so every non-null `parent_session_id` is an exact same-user/source `sessions.session_id`. Legacy raw parent UUIDs are preserved in `parent_thread_id` even when bytes are unavailable. | Child-before-parent, rotated-backfill, unresolved-parent, self-parent, first-leaf precedence, and zero-orphan graph-integrity tests pass. |
| H4 | `logpile/parsers.py`, `logpile/db.py`, `logpile/sync.py`, `logpile/stats.py`, `tests/test_parsers.py`, `tests/test_sync.py`, `tests/test_stats.py`, Claude sidechain fixtures | Claude agent identity and root parentage now use `isSidechain`, `agentId`, root `sessionId`, and `/subagents/` path evidence. Sidechains receive nonzero depth, canonical agent IDs, raw thread IDs, and exact canonical parents. Identity-version backfill reparses unchanged live or rotated rows. Stats defensively recognize depth, canonical parent, or subagent path evidence. Workflow `journal.jsonl` progress records are parsed for identity tests but excluded from sync so they cannot overwrite the full agent transcript or collide in shared storage; one legacy stem-keyed journal row is retired on resync. | Root, standard sidechain, workflow-path, classification, exact-parent, rotated-backfill, journal-collision, and repeated-sync regressions pass. |
| H5 | `logpile/stats.py`, `tests/test_stats.py`, period fixture | Supplying `since` or `until` switches all stats to inclusive UTC event-period semantics. Overview, pattern, and repository tokens and active-session counts come from `session_daily_effective`; one session counts once even across several days. Pattern/repo labels remain session metadata. Subagent tool breakdowns filter each tool's own UTC-normalized timestamp; null or malformed timestamps are excluded from bounded reports. Unbounded reports retain whole-session semantics. | `tests/test_stats.py`: 45 passed, including pre-range sessions active in-range, exclusion of post-range usage, multi-day distinct counts, cross-section token equality, offset timestamps, and null/malformed tool times. |
| M1 | `logpile/parsers.py`, `logpile/db.py`, `logpile/sync.py`, `tests/test_parsers.py`, `tests/test_sync.py`, cache fixtures, `README.md` | Claude fallback parsing selects the last iteration whose full `(input, cache creation, cache read, output)` tuple matches the top-level result. Added persisted transcript/native `cache_creation_unknown_input_tokens`. Every parser, daily row, claim occurrence, migration, upsert, and native refresh enforces `5m + 1h + unknown = cache_creation`; missing or contradictory splits become explicit unknown usage. | Matching-iteration, contradictory, absent-breakdown, daily, claim, persistence, migration, and native-subtype invariants pass. In the sampled fallback files, corrected known 1h attribution fell by 6,688 tokens; both had exact matching iterations, so no unknown remainder was needed there. |
| M2 | `logpile/parsers.py`, `logpile/db.py`, `logpile/sync.py`, `tests/test_parsers.py`, `tests/test_sync.py`, residual-day fixture, `README.md` | Added persisted `session_daily_usage.approximated`. After parsing, every token and message/tool count component is reconciled to the session total. Timestamp-less residuals use the first valid event day, falling back deterministically to file mtime, and timestamp-less claims use the same day. Database insertion rejects non-reconciling daily rows. `session_daily_effective` exposes the stored approximation flag and retains whole-session fallback only for legacy rows with no daily data. | Parser-wide daily invariants, mtime fallback, persisted residual flags, sync round trips, and session/daily equality tests pass. |
| M3 | `logpile/db.py`, `logpile/sync.py`, `tests/test_claims.py`, `tests/test_sync.py`, claim fixtures, `README.md` | Replaced winner-only claims with occurrences keyed by `(claim_key, session_id)`. `message_claim_owners` derives the minimum-ranked live claimant, while native aggregation joins the winning occurrence's own values. Applying claims replaces only that session's occurrences and refreshes every claimant for touched keys, so owner claim removal or rank changes promote an unchanged loser immediately. Legacy winners migrate as occurrences; token version 7 repopulates losers. Orphan cleanup and schema migration set `native_refresh_pending`, preserving interrupted-sync recovery. | `tests/test_claims.py`: 26 passed + 2 subtests. Coverage includes order independence, two retained occurrences, owner removal, rank changes without loser reparse, stale loser removal, winner-specific values, legacy migration, orphan promotion, and no-change interrupted-refresh healing. |

## Verification

- `.venv/bin/python -m pytest tests/ -q`: **266 passed, 9 subtests passed**.
- Focused: parsers 41 passed; sync 65 passed + 7 subtests; stats 45 passed; claims 26 passed + 2 subtests.
- Python compilation passed for `logpile/*.py` and `tests/test_*.py`.
- Fresh in-memory schema migrated twice idempotently: `PRAGMA integrity_check = ok`, no foreign-key findings, and the claims primary key is exactly `(claim_key, session_id)`.
- `git diff --check` passed. No Git write command was used.

## Read-only live-corpus spot check

The check parsed exactly 20 real files with both old and new logic, streaming
large Codex files to avoid whole-file buffering. Coverage was 6 Codex forks
(including multi-second copied prefixes), 4 fresh non-fork same-second files,
1 explicit-reset file, 7 Claude subagent paths, and 2 Claude fallback-mismatch
files. Unique sampled bytes totaled 2,215,226,449. No session text was emitted,
and no database or source file was modified.

Aggregate new-minus-old deltas across the 20-file sample:

| Metric | Delta |
|---|---:|
| Codex total input | -4,330,297,914 |
| Codex fresh input | -170,266,938 |
| Codex cached input | -4,160,030,976 |
| Codex output | -11,406,192 |
| Codex reasoning output | -3,299,975 |
| Codex user messages | -2,270 |
| Codex assistant messages | -1,334 |
| Codex tool calls | +4 |
| Claude sessions classified as subagents | +7 |
| Claude explicit parent references | +7 |
| Claude canonical identity changes | +2 |
| Claude known 1h cache-creation attribution | -6,688 |

The Codex net combines two corrections in opposite directions. Removing copied
fork history dominates the sample, while the one genuine reset independently
adds 8,666,721,598 input, 8,476,386,688 cached input, 15,160,072 output, and
4,370,103 reasoning tokens. The four fresh same-second files retained live
work, producing the positive net tool-call delta despite the fork removals.

## Expected corpus pattern-table shift

The dominant classification change should be Claude sidechains moving into
`subagent`: approximately 7,772 additional subagent sessions. About 7,595 of
those currently sit in `short-task`, carrying roughly 94.3M native output
tokens, so `short-task` should fall by about that many sessions/tokens before
accounting corrections. The remaining roughly 177 rows should move from other
root-pattern buckets.

Structural replay removal will offset part of the subagent token gain because
large Codex fork files currently retain inherited output. In this sample,
non-reset Codex corrections reduced output by about 26.6M after separating the
reset's +15.16M contribution; most sampled large forks are subagents. The reset
session itself is a root marathon, so `marathon` should gain about 15.16M
output. A reasonable directional estimate is therefore:

- `subagent`: roughly +7.8K sessions and a net output increase likely around
  +65M to +70M after the sampled replay offset, with corpus-wide fork coverage
  determining the final token value;
- `short-task`: roughly -7.6K sessions and about -94M reclassified output,
  partially offset by restored fresh-file usage;
- `marathon`: about +15.2M output from the recovered reset epoch;
- overall Codex totals: likely lower despite the reset recovery, because
  inherited multi-second fork usage is much larger in the sampled correction.

These are estimates only. The reviewer’s post-merge full resync should produce
the authoritative corpus pattern table.
