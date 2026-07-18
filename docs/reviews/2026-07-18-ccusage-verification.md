# Empirical verification of ccusage Codex token accounting

Date tested: 2026-07-18

Scope: ccusage Rust main at 7acee6c5853c26fe66fbe1453bd94c9376afec06 and the latest shipping npm package, ccusage 20.0.17. All ccusage and logpile repositories remained read-only. The pre-existing logpile modification at site/index.html was preserved untouched; ccusage remained clean. No GitHub issue, pull request, or comment was filed.

## Executive verdict

The current npm package is not lagging the relevant Rust implementation. npm ccusage 20.0.17 and Rust HEAD produce identical results on every requested fixture, and the relevant Codex parser/loader logic in the release tag and HEAD is the same.

The current errors are:

1. Multi-second inherited fork replay is still counted as native usage. Both implementations overcount the requested modern replay fixture by 238,700,000 total tokens and its legacy counterpart by 238,900,050 total tokens.
2. Legacy cumulative-only records without a source total_tokens field overstate the reported totalTokens field by adding reasoning tokens a second time. This affects even cases whose input, cache, output, and reasoning components are otherwise correct.
3. The fork replay heuristic can also undercount: genuine fork-local usage is discarded when its first two token events happen in the same wall-clock second.
4. Daily/weekly/monthly value-based deduplication can collapse two independent sessions that happen to emit identical events. The session report keeps them separate.

The requested explicit-zero counter-reset, ordinary non-fork fresh-same-second, and parent/child same-second replay fixtures now have correct component accounting in both implementations and both formats. Their totalTokens values are fully correct only in the modern last_token_usage format; every legacy example with reasoning still triggers the separate total fallback error. These successes do not repair the multi-second case.

Therefore:

- A replay-boundary fix is justified against HEAD.
- A legacy totalTokens fallback fix is justified against HEAD.
- A session-blind dedupe fix is justified against HEAD, although its prevalence in the real corpus was not measured here.
- No PR is justified for the requested explicit-zero reset case: current code already handles it.
- The main finding is not npm release lag. It is behavior shared by npm 20.0.17 and HEAD.

## Versions and artifact identity

| Artifact | Exact identity | Evidence |
|---|---|---|
| Rust HEAD | 7acee6c5853c26fe66fbe1453bd94c9376afec06 | main and origin/main both pointed to this clean checkout; commit timestamp 2026-07-16T00:25:18+01:00 |
| npm latest | ccusage 20.0.17 | npm registry latest as checked 2026-07-18; published 2026-07-10T09:38:21.487Z |
| npm release tag | v20.0.17 at 88cdfa4fb201c92b163a34d0bbb097b68d3185cf | GitHub tag/release metadata |
| npm main tarball | SHA-1 90a58b54dab57cea608b85391eb4ff786fc3b2d3 | npm registry dist metadata |
| npm darwin-arm64 tarball | SHA-1 41ef0a3a177c41baaf5df1dac35c8eca482b0434 | npm registry dist metadata |
| Extracted shipping native binary | SHA-256 08c455a4307345ca2b0fcda3a81edd9421a7edd53ea0acea19309925a7af54c0 | Local hash; binary reports ccusage 20.0.17 |

The exercised npm launcher was:

    /Users/maxghenis/.npm/_npx/86c8a4e7f010dae6/node_modules/.bin/ccusage

The platform-native executable was:

    /Users/maxghenis/.npm/_npx/86c8a4e7f010dae6/node_modules/@ccusage/ccusage-darwin-arm64/bin/ccusage

The separately installed command at /Users/maxghenis/.bun/bin/ccusage reports 18.0.10. That is a stale local installation, not npm latest, and was not used as the shipping comparator.

The release tag's Codex parser.rs and loader.rs were directly compared with HEAD and are line-for-line identical: parser.rs is 1,001 lines and loader.rs is 1,383 lines. The native package's behavior independently confirms that its parser recognizes both last_token_usage and the current fork replay path.

Authoritative pages:

- npm: https://www.npmjs.com/package/ccusage/v/20.0.17
- release: https://github.com/ccusage/ccusage/releases/tag/v20.0.17
- HEAD commit: https://github.com/ccusage/ccusage/commit/7acee6c5853c26fe66fbe1453bd94c9376afec06

## Build and execution notes

I read the repository's root instructions and the Codex adapter README, plus the referenced development, Rust, testing, and agent-source instructions, before attempting the build.

The requested full workspace build could not complete in this network-restricted environment:

- cargo build attempted to acquire the repository-pinned Rust 1.96 toolchain and could not resolve static.rust-lang.org.
- cargo +stable build used the installed Rust/Cargo 1.94.1 but could not resolve crates.io.
- cargo +stable build --offline failed resolution because the local cache lacks insta. A resolution-only scratch shim exposed further uncached production dependencies, including minreq; Nix was not installed.

The final offline failure was:

    error: no matching package named insta found
    location searched: crates.io index
    required by package ccusage v0.0.0 (.../rust/crates/ccusage)

To test HEAD rather than substitute a reimplementation, I compiled a scratch Rust harness that imports these actual HEAD files by absolute path, unchanged:

- rust/crates/ccusage/src/adapter/codex/parser.rs
- rust/crates/ccusage/src/adapter/codex/types.rs

The harness supplies only the surrounding crate types/timestamp helpers needed to compile the parser, walks the real sessions tree, and applies loader.rs's exact event-deduplication tuple. Its binary is:

    /Users/maxghenis/.cache/ccusage-verify/head-parser-harness/target/debug/ccusage-head-parser-harness

Thus the matrix's “Rust HEAD” rows are direct executions of the HEAD parser/types plus the HEAD loader dedupe key, not a successful build of the complete ccusage CLI. The one-file core matrix is unaffected by report aggregation. The shipping native binary provides a second end-to-end check and matches the HEAD harness exactly. The extra cross-session probe separately exercises the shipping daily and session aggregation paths.

## Fixture construction

The source fixtures came from:

    /Users/maxghenis/logpile/tests/fixtures/codex/

They were copied into actual Codex rollout layouts under:

    /Users/maxghenis/.cache/ccusage-verify/fixtures/{modern,legacy}/{case}/sessions/YYYY/MM/DD/rollout-....jsonl

### Important format discovery

The three supplied scenario fixtures were already the requested legacy form: they contain total_token_usage only and no last_token_usage events. They were copied byte-for-byte:

| Fixture | SHA-256 |
|---|---|
| replay-multisecond.jsonl | 8706cf025793608e645ac2b71f8c1e56120210aae6c6f5a2dfdb5fb54e6f40fe |
| counter-reset-epochs.jsonl | 2d0ce9394e0224be60417e0c0346ed8d7070933abef6407c62a0edda19ca145b |
| fresh-same-second.jsonl | 94e8df60b846c5ff89eca75e6f7ab427a18994873c1a63682802d887d4dba1e0 |

The modern variants retain those cumulative fields and add per-event last_token_usage deltas. Vectors below are ordered as raw input, cached input, output, reasoning; replayed parent events still carry deltas even though they are not native to the child:

| Scenario | Added last_token_usage vectors |
|---|---|
| Multi-second replay | (100,000,000, 80,000,000, 1,000,000, 100,000); (136,700,000, 120,000,000, 1,000,000, 100,000); live (5,000, 3,000, 500, 50) |
| Counter reset | (1,000, 800, 100, 40); explicit zero; (900, 700, 60, 20) |
| Fresh same-second | (1,000, 600, 50, 10); (1,000, 800, 40, 10); (1,000, 1,000, 30, 10) |
| Parent/child | parent (100, 50, 10, 1); child replay (10,000, 8,000, 500, 100), (10,000, 8,000, 400, 100); child live (5,000, 2,000, 200, 50) |

For synthesized modern records, last_token_usage.total_tokens is raw input plus output. Reasoning is a subset of output, not a third billable component.

## Logpile oracle and derivation

The oracle was logpile's parser in the existing virtual environment. From /Users/maxghenis/logpile:

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -B -c 'from pathlib import Path; from logpile.parsers import parse_codex_session; root=Path("tests/fixtures/codex"); names=["replay-multisecond.jsonl","counter-reset-epochs.jsonl","fresh-same-second.jsonl","rollout-2026-05-01T00-00-00-parent-thread.jsonl","rollout-2026-06-08T11-39-04-leaf-thread.jsonl"]; print("fixture,fresh,cache,total_input,output,reasoning,ccusage_total"); [(lambda i: print(f"{n},{i.fresh_input_tokens},{i.cached_input_tokens},{i.total_input_tokens},{i.total_output_tokens},{i.reasoning_output_tokens},{i.fresh_input_tokens+i.cached_input_tokens+i.total_output_tokens}"))(parse_codex_session(root/n)) for n in names]'

The synthesized modern files were then parsed with the same parse_codex_session function and produced identical oracle totals.

| Scenario | Fresh input | Cache read | Raw input | Output | Reasoning | Correct total |
|---|---:|---:|---:|---:|---:|---:|
| Multi-second replay | 2,000 | 3,000 | 5,000 | 500 | 50 | 5,500 |
| Counter reset | 400 | 1,500 | 1,900 | 160 | 60 | 2,060 |
| Fresh same-second | 600 | 2,400 | 3,000 | 120 | 30 | 3,120 |
| Parent rollout | 50 | 50 | 100 | 10 | 1 | 110 |
| Child rollout | 3,000 | 2,000 | 5,000 | 200 | 50 | 5,200 |
| Parent + child tree | 3,050 | 2,050 | 5,100 | 210 | 51 | 5,310 |

Correct total is fresh input + cache read + output, equivalently raw input + output. Reasoning is shown diagnostically and is not added again.

### Oracle arithmetic

Multi-second replay:

- Inherited terminal baseline: (236,700,000 raw input, 200,000,000 cached, 2,000,000 output, 200,000 reasoning).
- Live cumulative event: (236,705,000, 200,003,000, 2,000,500, 200,050).
- Native delta: (5,000, 3,000, 500, 50).
- Fresh input: 5,000 − 3,000 = 2,000.

Counter reset:

- Sum the two explicit epochs: (1,000, 800, 100, 40) + (900, 700, 60, 20) = (1,900, 1,500, 160, 60).
- Fresh input: (1,000 − 800) + (900 − 700) = 400.

Fresh same-second:

- This file has no fork structure, so all progress is native.
- Terminal values are (3,000, 2,400, 120, 30); fresh input is 600.

Parent/child:

- Child inherited baseline: (20,000, 16,000, 900, 200).
- Child live cumulative: (25,000, 18,000, 1,100, 250).
- Child-native delta: (5,000, 2,000, 200, 50).
- Add the independently stored parent's (100, 50, 10, 1) to obtain the tree total.

Relevant logpile evidence:

- Prior findings: /Users/maxghenis/logpile/docs/reviews/2026-07-11-sol-full-review.md:23-33
- Cumulative extraction: /Users/maxghenis/logpile/logpile/parsers.py:946-975
- Structural replay/native boundary: /Users/maxghenis/logpile/logpile/parsers.py:1852-1866
- Replay baseline and reset epochs: /Users/maxghenis/logpile/logpile/parsers.py:1930-1965
- Component deltas and fresh input: /Users/maxghenis/logpile/logpile/parsers.py:1965-1986
- Final invariant: /Users/maxghenis/logpile/logpile/parsers.py:2053-2087
- Fixture assertions: /Users/maxghenis/logpile/tests/test_parsers.py:1226-1277 and 1297-1305
- Logpile accounting fix: 9070e5006a47a000e6249cb775fdfd87318b8bc9, “review batch 2: accounting core”

## Required 12-cell matrix

Tuple order is:

    (fresh input, cache read, raw input, output, reasoning, totalTokens)

Both comparators were run offline with UTC report grouping and no cost lookup. Every row below is measured, not inferred from source equivalence.

| Implementation | Format | Scenario | Oracle tuple | Measured tuple | Verdict |
|---|---|---|---|---|---|
| Rust HEAD parser | Modern last_token_usage | Multi-second replay | (2,000, 3,000, 5,000, 500, 50, 5,500) | (36,702,000, 200,003,000, 236,705,000, 2,000,500, 200,050, 238,705,500) | Overcounts total by 238,700,000 |
| Rust HEAD parser | Modern last_token_usage | Counter reset | (400, 1,500, 1,900, 160, 60, 2,060) | (400, 1,500, 1,900, 160, 60, 2,060) | Correct |
| Rust HEAD parser | Modern last_token_usage | Fresh same-second | (600, 2,400, 3,000, 120, 30, 3,120) | (600, 2,400, 3,000, 120, 30, 3,120) | Correct |
| Rust HEAD parser | Legacy cumulative-only | Multi-second replay | (2,000, 3,000, 5,000, 500, 50, 5,500) | (36,702,000, 200,003,000, 236,705,000, 2,000,500, 200,050, 238,905,550) | Overcounts total by 238,900,050 |
| Rust HEAD parser | Legacy cumulative-only | Counter reset | (400, 1,500, 1,900, 160, 60, 2,060) | (400, 1,500, 1,900, 160, 60, 2,120) | Components correct; total over by 60 |
| Rust HEAD parser | Legacy cumulative-only | Fresh same-second | (600, 2,400, 3,000, 120, 30, 3,120) | (600, 2,400, 3,000, 120, 30, 3,150) | Components correct; total over by 30 |
| npm 20.0.17 | Modern last_token_usage | Multi-second replay | (2,000, 3,000, 5,000, 500, 50, 5,500) | (36,702,000, 200,003,000, 236,705,000, 2,000,500, 200,050, 238,705,500) | Overcounts total by 238,700,000 |
| npm 20.0.17 | Modern last_token_usage | Counter reset | (400, 1,500, 1,900, 160, 60, 2,060) | (400, 1,500, 1,900, 160, 60, 2,060) | Correct |
| npm 20.0.17 | Modern last_token_usage | Fresh same-second | (600, 2,400, 3,000, 120, 30, 3,120) | (600, 2,400, 3,000, 120, 30, 3,120) | Correct |
| npm 20.0.17 | Legacy cumulative-only | Multi-second replay | (2,000, 3,000, 5,000, 500, 50, 5,500) | (36,702,000, 200,003,000, 236,705,000, 2,000,500, 200,050, 238,905,550) | Overcounts total by 238,900,050 |
| npm 20.0.17 | Legacy cumulative-only | Counter reset | (400, 1,500, 1,900, 160, 60, 2,060) | (400, 1,500, 1,900, 160, 60, 2,120) | Components correct; total over by 60 |
| npm 20.0.17 | Legacy cumulative-only | Fresh same-second | (600, 2,400, 3,000, 120, 30, 3,120) | (600, 2,400, 3,000, 120, 30, 3,150) | Components correct; total over by 30 |

### Replay component error

The modern and legacy replay cases have the same component inflation:

| Metric | Oracle | Measured | Error |
|---|---:|---:|---:|
| Fresh input | 2,000 | 36,702,000 | +36,700,000 |
| Cache read | 3,000 | 200,003,000 | +200,000,000 |
| Raw input | 5,000 | 236,705,000 | +236,700,000 |
| Output | 500 | 2,000,500 | +2,000,000 |
| Reasoning | 50 | 200,050 | +200,000 |

Modern totalTokens is inflated by the inherited raw input plus output, 238,700,000. Legacy totalTokens adds the full measured reasoning amount again, making the total error 238,900,050.

### Correct-cell attribution summary

| Correct cells | Logic/credit |
|---|---|
| Modern counter reset, HEAD and npm | last_token_usage is consumed directly at parser.rs:268-276, so cumulative resets do not erase native deltas. Earliest verified release credit is the Rust-first v20.0.0 tag at 635b5c7ac55edc186d6eb766e680ebf33272c615; a narrower feature commit could not be recovered responsibly. |
| Modern fresh same-second, HEAD and npm | The structural replay gate at parser.rs:67-76 requires thread_spawn or forked_from_id. The non-fork fixture never enters the same-second skip path at parser.rs:143-146. |
| Fully correct legacy cells | None: reset and fresh have correct components but their reported totalTokens values trigger the independent reasoning fallback bug. |
| Modern parent/child supplement, HEAD and npm | PR #1218 / 38c883b9 introduced same-second thread_spawn replay skipping; PR #1369 / 50d3444 added forked_from_id recognition. |

## Supplemental parent/child pair

The requested parent/child rollout pair confirms what the new heuristic does fix:

| Implementation | Format | Oracle | Measured | Verdict |
|---|---|---|---|---|
| Rust HEAD parser | Modern | (3,050, 2,050, 5,100, 210, 51, 5,310) | Exact oracle | Correct |
| npm 20.0.17 | Modern | (3,050, 2,050, 5,100, 210, 51, 5,310) | Exact oracle | Correct |
| Rust HEAD parser | Legacy | (3,050, 2,050, 5,100, 210, 51, 5,310) | (3,050, 2,050, 5,100, 210, 51, 5,361) | Components correct; total over by 51 |
| npm 20.0.17 | Legacy | (3,050, 2,050, 5,100, 210, 51, 5,310) | (3,050, 2,050, 5,100, 210, 51, 5,361) | Components correct; total over by 51 |

## Mechanisms, source references, and credit

For concise references below, C is:

    /private/tmp/claude-501/-Users-maxghenis/1328bb0e-b3c8-40e4-8681-49fb790d3b45/scratchpad/ccusage

### 1. Multi-second replay overcount

The parser first marks a file replay-eligible only if thread_spawn or forked_from_id appears in its first 16 KiB:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:67-76

detect_replay_second then examines the first two qualifying token_count events. It returns a replay second only if their timestamps share the same first 19 bytes, i.e. the same wall-clock second. If the second event is in another second, it returns None immediately:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:78-136
- Decisive branch: parser.rs:123-132

The requested inherited events occur at 11:39:04.900 and 11:39:06.100. The function therefore returns None. No replay baseline is established and the ordinary parser consumes all three events.

When last_token_usage exists, it is preferred verbatim:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:268-279

That makes both inherited modern deltas look native. In legacy input, componentwise cumulative subtraction telescopes to the inherited terminal cumulative total plus live usage:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:959-979

The skip loop itself is limited to the one detected second and seeds previous_totals only from events skipped in that second:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:143-203
- Baseline update: parser.rs:191-200

Credit and limits:

- PR #1218, implementation commit 38c883b9ba66eb280795a0f752e1d2b0b057eb16, introduced the thread_spawn same-second replay heuristic: https://github.com/ccusage/ccusage/pull/1218
- PR #1369, commit 50d3444a93b6d6185163ea995eb812c8ba1d904f, extended replay recognition to forked_from_id and is included in v20.0.17: https://github.com/ccusage/ccusage/pull/1369
- Those changes deserve credit for the supplied same-second parent/child case's correct component accounting and fully correct modern total. They do not recognize replay spanning seconds.

### 2. Legacy totalTokens double-counts reasoning

When a source record omits total_tokens, CodexRawUsage deserialization falls back to:

    input + output + reasoning

Source:

- C/rust/crates/ccusage/src/adapter/codex/types.rs:170-197
- Exact fallback: types.rs:193-196

Codex output_tokens already includes reasoning tokens. The correct total is input + output. Aggregation then sums the erroneous per-event total_tokens without correction:

- C/rust/crates/ccusage/src/adapter/codex/aggregate.rs:314-337

This explains all legacy-only total errors exactly:

- reset: +60, equal to reasoning
- fresh same-second: +30, equal to reasoning
- parent/child: +51, equal to reasoning
- replay: the replay error plus all 200,050 measured reasoning tokens

The component columns remain correct in the non-replay legacy cases because they use the separate raw fields; only totalTokens uses the bad fallback.

### 3. Why explicit-zero reset component accounting is correct

Modern events bypass cumulative-delta inference because last_token_usage is preferred:

- C/rust/crates/ccusage/src/adapter/codex/parser.rs:268-276

This last_token_usage-primary path is present in the Rust-first v20.0.0 release, tag commit 635b5c7ac55edc186d6eb766e680ebf33272c615. The shallow checkout and unavailable GitHub file-history endpoint did not permit responsible attribution to a narrower feature commit, so the current logic and earliest verified release are credited here rather than guessing a PR.

For legacy input, the key is operation order. The parser replaces previous_totals with every cumulative snapshot, including the all-zero snapshot, before it drops an all-zero usage event:

- Baseline replacement: C/rust/crates/ccusage/src/adapter/codex/parser.rs:277-279
- Zero event dropped afterward: parser.rs:283-288

The next nonzero cumulative snapshot is therefore subtracted from zero and becomes the first event of a new epoch. That current state-machine logic, rather than PR #1369, is what fixes the requested legacy reset fixture.

This result is deliberately scoped to the explicit-zero fixture. A downward counter transition without an intervening zero is ambiguous and was not established as a distinct epoch by the logpile oracle in this audit.

### 4. Why ordinary fresh same-second component accounting is correct

The fresh fixture contains two legitimate snapshots in the same second but has no thread_spawn or forked_from_id marker. is_codex_replay_session returns false, so detect_replay_second is never used:

- Gate: C/rust/crates/ccusage/src/adapter/codex/parser.rs:67-76
- Conditional call: parser.rs:143-146

All events are counted. The structural fork gate is the relevant fix: timestamp coincidence alone no longer causes the ordinary non-fork fixture to be discarded.

## Additional empirical correctness probes

These probes are outside the required 12 cells but expose errors in the same current code.

### Genuine fork-local same-second usage is discarded

I added only forked_from_id metadata to the otherwise-correct fresh-same-second fixture. The oracle remains:

    (600, 2,400, 3,000, 120, 30, 3,120)

Both HEAD and npm report:

| Format | Measured | Error |
|---|---|---|
| Modern | (0, 1,000, 1,000, 30, 10, 1,030) | Undercounts total by 2,090 |
| Legacy | (0, 1,000, 1,000, 30, 10, 1,040) | Undercounts total by 2,080; remaining total also double-adds 10 reasoning tokens |

The heuristic sees a fork marker and two first events in the same second, declares that second inherited replay, and skips both. It has no structural boundary that distinguishes copied history from genuine usage. The PR #1369 review explicitly warned about this false-positive mode before merge.

Mechanism:

- Replay eligibility: parser.rs:67-76
- Same-second declaration: parser.rs:78-136
- Whole-second skip: parser.rs:175-203

Any replay repair should test both directions together: remove inherited multi-second history, but retain genuine fork-local events even when they share the replay second.

### Independent identical sessions collide in non-session reports

I created two unrelated modern sessions. Each emits one event at the same timestamp with the same model and token values:

    per session: (80, 20, 100, 10, 2, 110)
    correct aggregate: (160, 40, 200, 20, 4, 220)

Measured results:

| Report | npm 20.0.17 | Verdict |
|---|---|---|
| codex session | Two rows; aggregate (160, 40, 200, 20, 4, 220) | Correct |
| codex daily | One event; aggregate (80, 20, 100, 10, 2, 110) | Undercounts by half |
| HEAD loader harness | One event; aggregate (80, 20, 100, 10, 2, 110) | Undercounts by half |

The general loader key includes timestamp, model, and token values but omits session identity:

- C/rust/crates/ccusage/src/adapter/codex/loader.rs:125-137

A test explicitly codifies collapsing matching events from distinct sessions:

- C/rust/crates/ccusage/src/adapter/codex/loader.rs:163-170

The optimized aggregation key includes session identity only for session reports and substitutes zero for daily, weekly, and monthly reports:

- C/rust/crates/ccusage/src/adapter/codex/aggregate.rs:374-397
- Session-only branch: aggregate.rs:380-384

This logic was introduced to remove copied branch/goal history, notably PRs #1156 and #1237:

- https://github.com/ccusage/ccusage/pull/1156
- https://github.com/ccusage/ccusage/pull/1237

The intent is valid, but value equality is not a unique provider-event identity. A fix must preserve copied-history deduplication while preventing independent sessions from colliding. This audit proves the counterexample; it does not estimate how often it occurs in the user's real corpus.

## Recommended action

### PR 1: replace the one-second fork replay heuristic

This is the highest-priority accounting fix. A structural native/replay boundary should:

- recognize inherited prefixes spanning multiple seconds;
- preserve the cumulative inherited endpoint as the live baseline;
- retain genuine fork-local events in the same second;
- work for both modern last_token_usage and legacy cumulative-only files;
- keep the supplied parent/child component behavior and modern total correct;
- include both the multi-second overcount and same-second false-positive fixtures.

Merely widening the skipped time window would trade one error for another.

### PR 2: correct the legacy totalTokens fallback

When total_tokens is absent, derive it as input + output, not input + output + reasoning. Add a regression with nonzero reasoning and no total_tokens. This is small, independent, and directly justified against HEAD.

### PR 3: make dedupe provenance-aware

Do not use only timestamp/model/token equality as a globally unique event identity. Options include removing copied prefixes structurally before aggregation, retaining explicit parent/fork provenance, or including session identity where cross-session equivalence has not been proved. Tests should cover:

- copied parent history counted once;
- two independent identical sessions counted twice;
- daily and session reports reconciling on that independent-session fixture.

Treat priority as lower than the replay and total fallback fixes until real-corpus collision frequency is measured, but the correctness bug itself is demonstrated.

### No reset PR for this fixture

Do not file a PR claiming that the current parser misses the supplied explicit-zero reset epochs. Both modern and legacy component accounting are correct. The only error in the legacy reset cell is the independent totalTokens/reasoning fallback.

### Release decision

Do not characterize the current result as npm release lag. npm 20.0.17 already contains the Rust parser, last_token_usage path, same-second replay handling, and forked_from_id support, and it matches HEAD exactly here. A future npm release is needed only after new fixes land.

## Exact version scope for the tokenmaxxing paper

Avoid a timeless statement that “ccusage” generally misses reset epochs or always handles fork replay. The behavior changed across versions and is format-dependent.

Recommended current-version wording:

> We tested ccusage npm v20.0.17 and Rust main at commit 7acee6c5853c26fe66fbe1453bd94c9376afec06 on 2026-07-18. These versions correctly count the token components of explicit-zero counter epochs, ordinary non-fork same-second snapshots, and same-second inherited prefixes in the supplied parent/child case; totalTokens is also correct for the modern last_token_usage versions of those fixtures. They still count inherited fork history that spans multiple seconds as native usage, and their replay heuristic can discard genuine fork-local events in the nominated replay second. For cumulative-only records lacking total_tokens, their reported totalTokens double-counts reasoning, and non-session value-based deduplication can collide independent identical events.

Recommended historical wording for last week's result:

> The ccusage version audited on 2026-07-11 missed both the multi-second fork-replay fixture and the explicit-zero reset-epoch fixture. The reset finding is historical and should not be attributed to npm v20.0.17 or Rust main at 7acee6c; the multi-second replay finding still applies to both.

If the paper uses “ccusage-class counting” as a methodological family rather than a product/version name, define it operationally and attach the version:

> “ccusage-class” here means the endpoint/delta and value-deduplication logic exercised in ccusage v20.0.17 / 7acee6c, not all past or future ccusage releases.

Do not identify the 2026-07-11 comparator as 18.0.10 solely because that stale binary is installed today. Verify the prior audit's actual invocation or package lock first. If it cannot be recovered, call it “the version audited on 2026-07-11,” not a guessed version.

## Reproduction commands

Shipping npm:

    env CODEX_HOME=/Users/maxghenis/.cache/ccusage-verify/fixtures/modern/replay \
      /Users/maxghenis/.npm/_npx/86c8a4e7f010dae6/node_modules/.bin/ccusage \
      codex daily --json --offline --no-cost --timezone UTC

Rust HEAD parser harness:

    /Users/maxghenis/.cache/ccusage-verify/head-parser-harness/target/debug/ccusage-head-parser-harness \
      /Users/maxghenis/.cache/ccusage-verify/fixtures/modern/replay

Replace modern/replay with each matrix case. Supplemental probes are under:

    /Users/maxghenis/.cache/ccusage-verify/extra/fork-fresh
    /Users/maxghenis/.cache/ccusage-verify/extra/fork-fresh-modern
    /Users/maxghenis/.cache/ccusage-verify/extra/identical-independent

## Limitations

- The complete Rust workspace could not be built because the sandbox had neither network resolution nor all required cached crates. The parser/types under test were nonetheless the actual HEAD files, compiled unchanged, and the shipping binary agreed on every matrix result.
- “Correct” is fixture-specific and means agreement with logpile's reviewed accounting parser and the explicit arithmetic above.
- This audit did not score costs, pricing tables, date bucketing outside UTC, or non-Codex agents.
- The independent-session collision is a constructed minimal counterexample; real-corpus prevalence was not measured.
- No ccusage or logpile source was modified, and no GitHub write action was taken.
