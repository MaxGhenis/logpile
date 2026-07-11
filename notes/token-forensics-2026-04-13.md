# Token Forensics: Codex vs Claude Code

> Quoted session prompts and the session id were redacted 2026-07-11 ahead of publication; all figures and analysis are unchanged.


Date: 2026-04-13

## Question

Straude reported extremely high weekly token usage for `max`. Was that real, and if so, where was it coming from?

## What we found

- Before this investigation, Logpile was only ingesting token counts from Claude Code.
- Claude's weekly total looked huge, but it was mostly cached input, not fresh prompt text.
- Codex sessions did in fact contain token accounting in `event_msg.payload.type == "token_count"`, but Logpile was not parsing it.
- After backfilling Codex token totals, Codex dominated the last 7 days of usage.

## 7-day snapshot after Codex backfill

- Total across both sources: ~21.36B tokens
- Codex: ~20.58B total
- Claude Code: ~0.55B total

### Input mix

- Codex fresh input: ~10.52B
- Codex cached input: ~10.01B
- Claude fresh input: ~41K
- Claude cached input: ~548.05M

## Important interpretation

This does **not** look like a simple runaway hook.

It looks much more like:

- giant long-lived parent threads
- subagents inheriting massive parent context
- sessions reading large local corpora and then paying that context cost repeatedly

## Evidence

### Claude Code

The biggest Claude session was the "plot my Codex and Claude Code messages from JSONL histories" investigation.

- It had only hundreds of fresh input tokens.
- Nearly all of its token volume came from cached input.
- That means the agent was repeatedly reusing an already huge context, not receiving enormous new prompts every turn.

### Codex

The biggest Codex sessions were mostly `system_generated` or child sessions with `parent_session_id` populated.

Examples:

- a parent session with ~793M input tokens and ~4.5K tool calls
- multiple child sessions with ~620M input tokens and 1-14 tool calls

That is strong evidence for inherited context explosion:

- the child sessions did almost no work themselves
- but still carried enormous prompt/context footprints from the parent thread

## Product value proved here

This was a good Logpile investigation because the answer required joining several layers:

1. source-specific parser behavior
2. per-session token accounting
3. fresh vs cached input split
4. thread lineage / subagent parent links
5. real local corpus evidence instead of anecdotal intuition

Without Logpile, this looked like either:

- "Straude is broken"
- "Claude is secretly costing a fortune"
- or "some hook is going haywire"

With Logpile, the answer became much more specific:

- Claude's huge number was real but mostly cache reuse
- Codex was the actual dominant token source
- the most suspicious pattern was inherited context in spawned / system-generated sessions

## Product implications

Logpile should probably surface:

- fresh vs cached token split
- parent-thread lineage
- spawned-session token burden
- sessions whose token count is very high relative to tool calls
- context-explosion warnings for child sessions

## Practical workflow implications

- avoid spawning subagents from already giant parent threads when possible
- prefer fresh child threads for bounded subtasks
- avoid asking agents to ingest giant raw corpora inside the same long-lived working session
- preprocess or summarize big local datasets before handing them to the coding agent

## Deeper root cause

After digging further, there are really two separate issues:

1. Raw Codex weekly totals are being overstated by forked child sessions.
2. Even after removing that duplication, a few root Codex sessions are still genuinely very large.

### 1. Forked child sessions are carrying parent cumulative token totals

Some Codex child session files include:

- a child `session_meta` with `forked_from_id`
- the inherited parent transcript
- inherited parent `token_count` events
- then the child's own new task

That means a child session can look like it used hundreds of millions of tokens even when it only did a small amount of new work.

For the last 7 days:

- raw Codex total: ~20.58B tokens
- Codex root sessions only: ~4.11B tokens
- Codex child sessions: ~16.47B tokens

So most of the scary weekly total is duplicated lineage burden, not independent new work.

### 2. The remaining root sessions are still real and still large

After stripping out child-session duplication conservatively, Codex still used about ~4.11B tokens in the last 7 days.

That remaining usage is concentrated in a few long-lived root Codex Desktop sessions with thousands of tool calls, for example:

- ~794.8M tokens / 4,461 tool calls
- ~742.1M tokens / 3,960 tool calls
- ~617.7M tokens / 2,513 tool calls

Those are not hook artifacts. They are genuine long-running workstreams.

### Claude hook question

The Claude `ExitPlanMode` hook is not the culprit here.

The hook calls OpenAI directly with `curl` for plan feedback; it does not invoke Codex. The explicit Codex bridge in Claude appears to be the `/codex` command, not a hidden automatic hook.

## Stronger conclusion

So the actual answer is:

- not a runaway Claude `/codex` hook
- not primarily a runaway local-file hook
- partly an accounting problem from forked Codex child sessions
- partly a real workflow pattern of giant long-lived root Codex sessions

That is a much better example of Logpile's value than the original raw total, because it separates:

- duplicated lineage burden
- real root-session usage
- cached vs fresh input
- explicit parent/child session structure

## 2026-04-14 revision

After continuing the investigation into the patched `ccusage` path, the earlier "~4.11B root-only" correction turned out to be too aggressive.

The mistake in that intermediate estimate was treating descendant burden too much like simple replay duplication. When we re-ran the audit with replay-prefix stripping but still counted the child's post-prefix deltas, the weekly picture changed substantially.

### Deduped weekly total still looks enormous

For April 8-14, 2026, replay-prefix deduplication still leaves roughly:

- total: ~80.87B tokens
- input: ~80.45B
- cached input: ~75.87B
- output: ~288.22M

So the scary Straude-scale number is not explained away by a small accounting fix.

### What was actually happening

The dominant pattern is:

- a long-lived root Codex workstream stays open for days or weeks
- many explorer or delegated child sessions fork from that root
- each child replays almost all of the parent's cumulative token history
- `ccusage`'s replay-prefix stripping removes that exact repeated prefix
- but the child then continues doing substantial new work on top of that huge inherited context

In other words:

- there **was** real fork replay duplication
- but many child sessions were also genuinely expensive **after** the duplicated prefix

### Concrete example

One April 8 child session (`(session id redacted)`) matched its immediate parent's cumulative totals for 14,104 token-count events out of 14,122.

So replay stripping is doing real work there.

But after that near-complete replay, the child still accumulated roughly another ~655M total tokens by the end of the session. That is not a phantom hook; it is real continuation cost on top of inherited context.

### Weekly burn is concentrated in a few root workstreams

For April 8-14, 2026, the biggest deduped root workstreams were:

- `(email-triage → tax-policy analysis root)`: ~32.69B across 28 sessions
- `(multi-repo triage root)`: ~12.82B across 38 sessions
- `(benefit-reform simulation root)`: ~12.63B across 25 sessions

Those top three roots alone account for ~58.14B tokens, about 72% of the week's deduped total.

### Updated conclusion

The best current explanation is:

- not a runaway Claude hook
- not mainly a hidden background process
- not "just" an accounting bug
- mostly a workflow pattern of long-lived high-context Codex root threads with many expensive forked children

So the operational problem is less "fix the parser" and more:

- avoid spawning large swarms from already giant roots
- checkpoint into fresh roots more often
- use subagents only for bounded tasks when the parent context is already huge
- watch cached-context burden as a first-class metric

### Concrete fork clusters

The biggest fork-heavy workstreams for April 8-14, 2026 looked like this:

- `(email-triage → tax-policy analysis root)`
  - root total: ~32.69B across 28 sessions
  - biggest children:
    - `Locke` explorer: ~1.63B, cached/input share ~94.4%
    - `Faraday` explorer: ~1.41B, cached/input share ~94.4%
    - `Nash` explorer: ~1.41B, cached/input share ~94.5%

- `(benefit-reform simulation root)`
  - root total: ~14.29B across 27 sessions
  - biggest children:
    - `Feynman` explorer: ~826M, cached/input share ~94.8%
    - `Kant` explorer: ~825M, cached/input share ~94.8%
    - `Mendel` explorer: ~750M, cached/input share ~94.7%

- `(multi-repo triage root)`
  - root total: ~14.16B across 40 sessions
  - biggest children:
    - `Hooke` explorer: ~657M, cached/input share ~96.0%
    - `Dirac` explorer: ~655M, cached/input share ~96.0%
    - `Ohm` explorer: ~589M, cached/input share ~95.9%

- `(data-calibration review root)`
  - root total: ~3.85B across 25 sessions
  - biggest children:
    - `Archimedes` explorer: ~369M, cached/input share ~96.3%
    - `Gibbs` explorer: ~367M, cached/input share ~96.4%
    - `Kepler` explorer: ~305M, cached/input share ~96.5%

- `(org proposal review root)`
  - root total: ~3.62B across 8 sessions
  - biggest children:
    - `Huygens` explorer: ~556M, cached/input share ~91.3%
    - `Boole` explorer: ~505M, cached/input share ~91.1%
    - `Chandrasekhar` explorer: ~501M, cached/input share ~91.0%

These are not invisible background tasks. They are concrete fork swarms on top of long-lived roots, and the cached/input share shows that most of the burden is inherited context rather than fresh prompt text.
