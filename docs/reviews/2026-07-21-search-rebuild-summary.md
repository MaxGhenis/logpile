# Extracted-text search rebuild

Date: 2026-07-21

## Outcome

Logpile's default `search` command now queries a local SQLite FTS5 index of
extracted session prose instead of grepping raw JSONL records. The index admits
structured session metadata plus explicit user/assistant plaintext only. Tool
calls and results, thinking/reasoning, encrypted content, images and other
non-text blocks, opaque base64 runs, and harness preambles do not enter the
normal search path. The former raw grep remains available only through the
explicit forensic `--raw` flag; raw search is rejected in public mode.

## Approach and architecture

- `session_search_documents` is the canonical external-content table. It
  stores one structured field or one streamed transcript text block per row,
  keyed by session, field label, and chunk index.
- `session_search_fts` is an FTS5 external-content index over separate
  `structured_text` and `transcript_text` columns. FTS5 `bm25()` uses a 20:1
  structured/body weight, and an explicit structured tier guarantees that a
  repeated body term cannot outrank a goal or summary hit.
- `session_search_state` binds each completed revision to the search schema
  version, session file hash, structured metadata hash, status, and (for
  public sessions) reviewed artifact hash.
- Search joins `session_catalog`, uses `listed_public` for public mode, and
  otherwise exposes all sessions to the private operator. Public results also
  require a complete current state whose artifact hash and reviewed metadata
  still match the publication record.
- Public indexing reads from `open_verified_public_artifact`, consuming the
  same hash-checked `O_NOFOLLOW` descriptor used by the B7 serving path.
  Private/unlisted incremental indexing reads the hash-verified managed copy,
  not a mutable source file after archival verification.
- Transcript extraction iterates JSONL records and inserts one message text
  block at a time. It never buffers a whole transcript, including the corpus's
  files larger than 1 GiB.
- FTS replacement is savepoint-atomic per session. Sync commits live reparses
  every 50 sessions, rotated parser backfills every 200, and the final FTS
  backfill every 50. `search_refresh_pending` and per-session version/hash
  state make interruption resumable.
- `search_fts_generation` is made durably `rebuilding:<version>` before a
  derived-index reset. An interrupted virtual-table recreation therefore
  resets safely on the next sync instead of trusting complete state beside an
  empty FTS term index.

## Extraction and title rules

The Python title cleaner ports `web/src/lib/format.ts`'s `sessionTitle`
contract: a leading `recommended_plugins` title is empty, and up to four other
leading XML-style harness blocks are removed. Transcript cleanup separately
preserves real operator prose after a *closed* wrapper, while dropping an
unclosed plugin catalog whose boundary is unknowable.

Claude accepts string content and explicitly typed `text` blocks. Codex also
accepts `input_text` and `output_text`, plus the exact legacy bare
`{"text": ...}` shape. Untyped blocks with any additional tool/reasoning/image
keys are excluded. Token version 8 reparses version-7 structured titles so the
same allowlist governs `first_user_message` and its derived `session_goal`.

Base64 cleanup handles long continuous standard/URL-safe tokens, padded
tokens, newline wrapping at arbitrary widths, and canonical fixed-width
space/tab wrapping. Validation uses canonical decode/re-encode checks, with a
linear scanner for large horizontally wrapped runs.

## Files touched

| File | Change |
|---|---|
| `logpile/search.py` | New extracted-text indexer, resumable backfill, grouped/ranked FTS query, snippets, and fail-closed state checks |
| `logpile/db.py` | Search schema/version, external-content triggers, stale/cleanup triggers, and interruption-safe generation reset |
| `logpile/parsers.py` | Streaming plaintext allowlist, title/transcript harness cleanup, and base64 filtering |
| `logpile/sync.py` | Incremental indexing from verified copies, version-8 title healing, pending-state recovery, and chunked backfill |
| `logpile/cli.py` | Safe FTS default, new result format and `--public`, with legacy grep behind `--raw` |
| `tests/test_search.py` | Ranking, extraction, base64, title, visibility, artifact, race, recovery, resync, removal, grouping, and snippet regressions |
| `tests/test_cli_backends.py` | Safe-default/raw backend behavior and public/raw rejection |
| `tests/test_sync.py` | Parser-version migration expectation |
| `README.md` | Extracted search semantics and forensic raw-mode boundary |

## Test evidence

- `.venv/bin/python -m pytest tests/ -q`: **325 passed, 11 subtests passed**.
- Final focused extractor/search/CLI run: **74 passed**.
- Search/visibility/sync/recovery group: **104 passed, 7 subtests passed**.
- `ruff check` on every changed Python file: passed.
- `python -m compileall -q logpile tests`: passed.
- Independent review exercised stale revisions, artifact substitution,
  post-copy source races, missing/corrupt FTS recreation, tool-shaped untyped
  blocks, base64 wrapping variants, structured-vs-body ranking, and
  adversarial large-run performance. No actionable finding remained after the
  final cycle.

## Live corpus backfill

Preflight counted 29,758 sessions and about 50.05 GiB in stored live-path file
sizes, plus about 1.63 GiB of rotated/shared-only Claude transcripts whose
legacy `file_size` is zero. An extraction-only sample initially projected
roughly 6–12 minutes, but the required version-8 title safety migration also
reparsed parser/accounting state. That prerequisite was projected at 2–4
hours. The final version-2 FTS-only pass was projected at 15–30 minutes (30–45
minutes prudent upper allowance).

The full operation exceeded 20 minutes, so all three phases are
chunked/resumable as described above. A pre-existing scheduled usage-tracker
sync acquired `logpile.db.sync.lock` while implementation was underway and
performed the version-8 prerequisite. The final version-2 run and its actual
timings are recorded below.

The scheduled prerequisite ran from 05:41:49 to 06:33:54 EDT: **52m 05s**.
It upgraded parser-derived state and built a disposable version-1 search
generation. The next sync detected that generation, durably marked version 2
as rebuilding, and recreated the derived tables before indexing any v2 rows.

The measured final command was:

```console
$ /usr/bin/time -p .venv/bin/logpile sync --backend local \
    --db /Users/maxghenis/logpile/logpile.db \
    --shared /Users/maxghenis/logpile/shared -v
Search index: 27391 session(s), 46.52 GiB scanned in 334.8s; 0 missing, 0 error(s).
Local done: 6 new, 2406 updated, 20161 unchanged/skipped
real 439.08
user 355.16
sys 74.70
```

Thus the v2 extraction phase took **334.83s (5m 35s)** and the complete final
sync took **439.08s (7m 19s)**. Combined with the prerequisite, the observed
upgrade occupied about **59m 24s** of wall time. The extraction phase beat its
6–12 minute sample projection slightly; the prerequisite and complete v2 sync
both finished well below their conservative upper projections.

The corpus grew while the first sync was running, from the 29,758-session
preflight to **29,795 sessions**. Of those, 2,404 were already indexed by the
incremental live-scan path before the final backfill loop, which is why the
backfill line reports 27,391 refreshed sessions. The backfill's
`search_backfill_last_bytes` is 49,955,260,508 bytes (46.52 GiB); the ledger's
current nonzero `file_size` sum is 53,782,188,244 bytes (50.09 GiB), and the
7,282 legacy zero-size rotated rows account for the additional shared-only
corpus described above.

Post-run checks found:

- `PRAGMA quick_check`: `ok`.
- 29,795/29,795 sessions have version-2 search state and status `complete`.
- 0 missing states, 0 stale file/version/status states, 0 missing transcripts,
  0 indexing errors, and 0 orphan document/state rows.
- 1,224,382 document rows across 29,795 distinct sessions.
- `search_fts_generation=2`, `search_index_version=2`,
  `search_refresh_pending=0`, and `native_refresh_pending=0`.
- The target session has 52 extracted documents and a complete v2 state.
- The live catalog currently has zero `listed_public` sessions; mixed public
  and private visibility is therefore exercised by the dedicated fixture test.

## Real query output

The required query now puts the intended session first. The shown text is the
actual default CLI output; an automated check over all five returned snippets
also found **zero** 64+-character base64/opaque runs.

```console
$ .venv/bin/logpile search "yc's paxel tool" --backend local \
    --db /Users/maxghenis/logpile/logpile.db --limit 5
2026-07-11  fedffb9b-b20c-4e80-a14f-2988d0bff002  [user transcript]  maxghenis
was this the big session for rebuilding logpile? is it the live thing now? what about [yc's paxel tool]

2026-07-21  rollout-2026-07-21T04-52-07-019f83e0-1101-7563-9b31-34a05372a02d  [assistant transcript]  logpile
… session for rebuilding logpile? is it the live thing now? what about [yc's paxel tool]”. That makes the missing acceptance command almost certainly `logpile search paxel`: a …

2026-07-11  1328bb0e-b3c8-40e4-8681-49fb790d3b45  [assistant transcript]  maxghenis
… session for rebuilding logpile? is it the live thing now? what about [yc's paxel tool]"* — and that session answered it and filed the findings into the launch …

2026-07-21  rollout-2026-07-21T04-52-03-019f83e0-00b0-7632-ba3e-5e696dc66f1f  [assistant transcript]  logpile
… refresh heals on an unchanged subsequent sync. - An apostrophe query such as `[yc's paxel tool]` succeeds; unescaped FTS5 `MATCH` raises a syntax error, so quote and escape …

2026-07-21  rollout-2026-07-21T04-51-59-019f83df-f32c-7893-9639-62f7f81c1b02  [user transcript]  logpile
… returned base64 blobs while the real hit — session fedffb9b-b20c-4e80-a14f-2988d0bff002, whose final user message asks about "[yc's paxel tool]" — was absent). Read logpile/cli …
```

The target is private. A separate `--public --json` assertion verified that
its session ID is absent from public-mode results. Because the live catalog
currently has no listed-public rows, the nontrivial leak proof is
`test_public_mode_cannot_search_private_session_text`, which indexes a
searchable public fixture beside a private fixture and proves only the public
row can be returned.

Two additional real queries show a structured hit and ordinary transcript
hits, with match-centered context instead of JSONL records:

```console
$ .venv/bin/logpile search "firstmarriagefitaudit" --backend local \
    --db /Users/maxghenis/logpile/logpile.db --limit 3
2026-07-20  49339422-2116-4904-9a0a-86fdbb75a637  [first user message]  social-security-model/model
… add __reduce__ to [FirstMarriageFitAudit] mirroring RankRefreshFitAudit's (the in-tree precedent that solved

2026-07-20  rollout-2026-07-20T06-28-51-019f7f12-42e1-7643-a534-0ab00a837bdd  [assistant transcript]  social-security-model/sol-c2-fix2
… Only production change is `[FirstMarriageFitAudit].__reduce__`; protected candidate-1/16, gates, runs, frozen, and design surfaces are byte-identical. - `pytest …::test_fit_audit_and_model_pickle_round_trip …

2026-07-20  rollout-2026-07-20T02-58-22-019f7e51-8ef2-7812-a438-ba194e90f131  [assistant transcript]  social-security-model/sol-c2-run6
Confirmed and reported to `/root`: - `[FirstMarriageFitAudit].__post_init__` converts `checksums` to `MappingProxyType`. - During first seed, draw 0, household-side assembly, `_fit_digest(inputs.family)` calls `pickle.dumps`. - Pickle …
```

```console
$ .venv/bin/logpile search "MicroSeries map_to" --backend local \
    --db /Users/maxghenis/logpile/logpile.db --limit 3
2026-05-09  rollout-2026-05-08T20-44-24-019e0a68-403b-7260-ba10-155d7d33d49d  [assistant transcript]  maxghenis
… KYPA has one `np.array` in a non-microsimulation chart interpolation helper, not in the microsim paths; the PolicyEngine calculations are using [MicroSeries/`map_to]`.

2026-05-24  rollout-2026-05-24T16-27-29-019e5bab-da47-7702-b45d-69bd2c8ad15d  [assistant transcript]  maxghenis
… I’m running the notebook cell itself now and comparing that choice against current [MicroSeries/map_to] patterns before deciding whether it’s actionable.

2026-05-20  rollout-2026-05-20T09-29-57-019e4594-24e5-70e0-af2e-c9e4ac3a1ff4  [user transcript]  maxghenis
… pinned PolicyEngine stack, repo-local data storage, OBR/HMRC road-fuel litre fiscal controls, fiscal-year rates, [MicroSeries/map_to] distribution weighting, and tests. Do not edit files …
```

## Post-review fixes (2026-07-21, cross-family gate)

Before merge, an independent sol (codex) adversarial review plus a fable
adjudication pass produced the following changes. Where they contradict
claims above, this section supersedes them.

**Confirmed and fixed:**

- *Stop-word latency*: the original single query evaluated `snippet()` and
  three b-tree joins for every matching document; `the` (953,807 postings)
  never finished. Search now runs one column-filtered FTS query per tier
  (ranked inside the FTS5 sorter with a 4,000-candidate cap), joins/filters
  only those candidates, and fetches snippets solely for the returned
  winners — through the same eligibility predicate as the candidate query,
  inside one read snapshot, so a reused rowid can never rebind an excerpt to
  other (possibly private) text. When a round yields fewer distinct eligible
  sessions than the requested limit, the cap deepens geometrically until the
  tier is exact: the cut is a performance floor and never decides membership,
  so dense private or stale documents cannot shadow eligible public matches.
  `the` completes in ~1.5 s. The 20:1 bm25 weights are gone — tier precedence
  is structural, so the "structured outranks body" guarantee no longer
  depends on weight tuning.
- *Public score channel*: bm25 statistics span the mixed-visibility corpus,
  so public-mode results now omit numeric scores (ordering remains
  score-based; that narrow residual channel is accepted and documented).
- *Injected text in the index*: Claude `isMeta` records, text blocks riding
  tool-result user records (system reminders and hook output), and codex
  `# AGENTS.md instructions` payloads are excluded — from transcripts and
  from the structured title/goal fields old parser versions stored them in.
  This was 40k+ live documents of harness boilerplate indexed as "user
  prose"; after the v4 re-extraction the only matches left are sessions
  whose operators genuinely typed the phrase.
- *Over-broad preamble stripping*: leading-tag removal now consults a
  harness-tag allowlist grounded in a corpus scan; an operator prompt like
  `<task>fix X</task>` stays searchable instead of indexing as empty. The
  Python side therefore intentionally diverges from the web `sessionTitle`
  strip-any-tag contract.
- *State clobber race*: replacement re-checks the stale-trigger column set
  inside its savepoint and defers (rolls back) if the sessions row changed
  after the snapshot, so a concurrent redaction can no longer be overwritten
  by a stale `complete` revision.
- *Batching*: per-session savepoints were autocommitting (Python's sqlite3
  opens no transaction for `SAVEPOINT`), making the "commits every 50
  sessions" claim false. The backfill now opens its batch transaction
  explicitly.
- *Test strength*: the public/private test now proves a reviewed public
  fixture IS returned for the same phrase a private session holds (and that
  its score is suppressed); hyphenated-term and user-authored-XML regression
  tests were added.

Extraction changes above shipped as `SEARCH_INDEX_VERSION = 6` across four
re-extractions (v3: transcript-side provenance filters; v4: AGENTS.md drop
extended to structured fields; v5: second-round review fixes; v6: the
bare-header AGENTS.md marker variant). The
interruption design got an unplanned live test: the v3 backfill was killed
mid-run by a host process exit at 27,824/29,823 sessions and resumed to
completion in 22 s of extraction on the next sync.

A second sol pass on the first round of fixes confirmed eight of ten
resolutions and produced new findings, dispositioned as follows:

- *Snippet rowid rebinding (blocker)*: fixed — one read snapshot spans
  candidates and snippets, and the snippet query re-applies the shared
  eligibility predicate instead of trusting bare rowids.
- *Public membership shadowing (blocker)*: fixed — public mode enumerates
  eligible document ids first and matches them exactly (rowid pushdown,
  loop fallback above 100k docs); private mode deepens the rank cut until
  enough distinct sessions qualify. The candidate cap can no longer change
  membership in either mode, pinned by tests.
- *Race test proved less than claimed*: fixed — the test now commits the
  redaction from a second connection in the real window (before the
  replacement savepoint) and asserts the stale invalidation survives.
- *Ragged multiline prose eaten as wrapped base64*: fixed — newline-wrapped
  removal requires MIME-shaped uniform line widths (≥16, last line no
  wider); `"first\nsecond\n…"` survives, uniform-width fixtures still strip.
- *Allowlist omissions*: fixed — heartbeat, turn_aborted, goal_context,
  subagent_notification added.
- *AGENTS.md prefix over-match*: fixed — the marker requires the header to
  continue with `for <dir>` or an `<INSTRUCTIONS>` block (both injected
  shapes exist in the live corpus; a `" for "`-only marker re-admitted
  1,847 bare-header payload documents at v5), so prose about AGENTS.md
  stays while every observed injection shape is dropped.
- *Single-record memory (rationale defeated)*: fixed — search extraction
  reads lines through a chunked bounded reader (64 MiB cap); oversized
  lines are counted and skipped, never materialized or decoded.
- *Drift guard over-checks review columns for private rows (low)*: accepted
  — a concurrent review-metadata update can cause one unnecessary deferred
  replacement; it is fail-closed and heals on the next pass.

**Reviewed and accepted as-is (with rationale):**

- *Empty-but-present FTS beside complete state*: no identified crash path
  produces it (the durable `rebuilding:` marker covers recreation; savepoints
  cover replacement); treated as a tamper scenario, remedy is clearing
  `search_fts_generation`. Follow-up candidate: a `sync --rebuild-search`
  flag.
- *Single-record memory*: JSONL parsing materializes one decoded line at a
  time; a pathological single-line multi-GiB record would spike, but the
  full 46.5 GiB live corpus (including >1 GiB files) backfilled without
  incident and the previous parser buffered whole files. Follow-up
  candidate: a per-line byte cap with a skip counter.
- *Base64 edge cases*: unbroken 64+-character opaque runs are removed
  without decode validation by design (identifiers and digests are
  indistinguishable from payloads at that length — this is now documented at
  the regex rather than implied to be validated); sub-16-character
  horizontally wrapped chunks can survive as noise. Neither crosses a
  privacy boundary; `--raw` covers both.
- *Codex AGENTS.md text in stored titles*: search no longer indexes those
  payloads anywhere; the polluted stored rows heal via the v9 reparse below.

A third round verified all round-2 closures and added:

- *Boundary-tie determinism (blocker)*: the deepening loop stopped once
  `limit` sessions were found even when the rank cut split an equal-score
  class, making membership/newest-first order depend on rowid order at the
  cut. The loop now fetches cap+1 raw candidates and deepens whenever the
  boundary is tied, before joining (chunked b-tree lookups). Pinned by
  test_boundary_score_ties_deepen_until_exhausted.
- *isMeta records as titles (medium)*: `parse_claudecode_session` now skips
  harness-injected records for `first_user_message` (counts unchanged);
  `SESSION_TOKEN_VERSION = 9` reparses stored rows — this also heals the
  AGENTS-polluted codex titles deferred above.
- *Snippet IN-list at extreme --limit (medium)*: winner ids now bind in
  20k chunks like every other id list.
- *AGENTS marker (lows)*: requires an absolute path after "for" (prose
  like "for beginners:" survives) and runs after wrapper stripping so a
  payload behind a leading harness wrapper is still recognized.
- *Uniform equal-length word pairs eaten as wrapped base64 (low)*: the
  wrap-width floor is 24 columns ("misunderstanding\nresponsibilities" is
  uniform at 16 and now survives; real encoders wrap at 60+).

Search extraction is `SEARCH_INDEX_VERSION = 7` for the marker/width
changes.
