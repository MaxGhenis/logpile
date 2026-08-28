# Subfleet task events

Logpile can join Subfleet's logical tasks and runs to the Claude Code sessions
and Codex threads it already indexes. The integration consumes only Subfleet's
credential-free schema-v1 spool:

```text
$SUBFLEET_STATE_DIR/integration-events/v1/run_<opaque-id>.jsonl
```

When `SUBFLEET_STATE_DIR` is unset, the default is
`~/chief-of-staff/state/subfleet/integration-events/v1`. Use
`logpile sync --subfleet-events-dir /exact/integration-events/v1` (or
`LOGPILE_SUBFLEET_EVENTS_DIR`) to select a different spool.

## Security boundary

The importer requires the final path components to be exactly
`integration-events/v1`, enumerates only direct `run_<24 hex>.jsonl` children,
and refuses either named directory when it is a symlink or non-directory. Event
files must be non-symlinked regular files with one hard link and no group/world
permissions. The importer never enumerates Subfleet's private run ledger,
prompts, outputs, auth state, account homes, or raw lane/workspace paths.

Parsing is schema-strict. Logpile stores only normalized allowlisted columns;
it does not store event JSON, spool paths, or unknown fields. A malformed line
or a record with extra fields is rejected independently without stopping
session sync.

## Storage and reconciliation

- `subfleet_events` stores the three v1 lifecycle records (`run.started`,
  `run.bound`, and `run.finished`) keyed by `event_id`. Each run may have one
  start, multiple native bindings, and one finish.
- `subfleet_attempts` stores one provider-native binding per `attempt_id` and
  enforces uniqueness for `(provider, native_id)`. Its `session_id` is nullable
  until the corresponding native transcript reaches Logpile.
- `subfleet_task_timeline` joins event, attempt, and selected session metadata.
  `logpile.subfleet.get_task_timeline()` is the Python query surface.
- `subfleet_task_catalog` and `logpile.subfleet.list_tasks()` provide
  recent-task discovery with provider and run/attempt counts plus the latest
  outcome.

Ingestion tolerates lifecycle records arriving out of order. Every sync retries
unresolved bindings after native transcripts are indexed, even when the spool
is empty or no longer exists. Replaying an identical `event_id` is idempotent;
replaying that ID with different normalized values is rejected. Timeline order
is deterministic: `occurred_at`, lifecycle order (started, bound, finished),
then `event_id`.

Subfleet may prune a run's spool file with its private ledger retention. Logpile
retains metadata it has already ingested; source pruning does not delete event
or attempt rows. Use Logpile's normal database backup workflow if this retained
task history must survive loss of the local database.

## Local query

```bash
./logpile.sh sync
./logpile.sh task-list
./logpile.sh task-list --json
./logpile.sh task-timeline task_<opaque-id>
./logpile.sh task-timeline task_<opaque-id> --json
```

The first integration slice is deliberately local-only. It does not add a web
or public API route because task membership and publication policy are separate
from the existing per-session visibility gate.

This consumer accepts only Subfleet's `run.*` schema-v1 events. Traycer bridge
receipts use separate `traycer.agent.*` event names, and Traycer's current
public create/list contract does not expose the provider-native harness session
ID needed for a safe transcript join. Bridge-created Traycer agents therefore
do not appear in this task timeline yet.
