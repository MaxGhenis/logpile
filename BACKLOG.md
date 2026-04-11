# Logpile Backlog

## Architecture

### 1. Retire dual backends

Problem:
`Flask` and the `Next` app both serve product behavior against the same SQLite database. Even with shared views and parity work, this creates ongoing drift risk.

Target:
Pick one product-serving backend.

Likely direction:
- Keep Python for ingestion, sync, enrichment, redaction, and publish review.
- Make the Next app the primary product surface.
- Retire Flask once Next reaches feature parity for private and public workflows.

Why it matters:
- Reduces duplicated business logic.
- Makes privacy and visibility rules easier to trust.
- Gives the frontend a single canonical contract.

### 2. Separate ingestion from serving cleanly

Problem:
The current local-first architecture mixes parsing/enrichment concerns with app-serving concerns.

Target:
Create a clearer split between:
- ingestion engine
- product app

Likely direction:
- Python package remains responsible for:
  - raw session parsing
  - sync/reindex
  - deterministic enrichment
  - publish review and redaction
- Web app remains responsible for:
  - product UI
  - editorial workflows
  - public profile and session surfaces

Why it matters:
- Makes the system easier to reason about.
- Preserves Python where it is strongest.
- Keeps the web app focused on product concerns rather than file parsing.

### 3. Plan the hosted data model now

Problem:
SQLite plus shared local files is the right shape for dogfooding, but not the long-term shape for a hosted multi-user product.

Target:
Define the hosted migration path before public usage expands.

Likely direction:
- Postgres for canonical structured metadata.
- Object storage for raw/redacted session artifacts.
- Background jobs for sync, enrichment, redaction scans, and reprocessing.
- Keep local-first ingest as a first-class mode.

Why it matters:
- Prevents the current local layout from hardening into the hosted architecture by accident.
- Makes future auth, profile publishing, and queue workflows easier to implement cleanly.

## Product

### 4. Finish the publish workflow

Problem:
The backend review and queue machinery now exists, but the product still needs a strong editorial publish flow in the new frontend.

Target:
Make public sharing feel deliberate and review-driven instead of like toggling a raw log public.

Current pieces already in place:
- deterministic session summaries
- session status classification
- publish queue APIs
- review APIs
- visibility controls

Missing pieces:
- queue UI
- review UI
- publish preview
- redaction-first publishing flow

### 5. Keep public sharing narrow at first

Problem:
The legal and product trust risk is mostly around publishing sensitive session content too quickly.

Target:
Roll out public profiles and public sessions gradually.

Initial policy:
- Local-first by default.
- Public profile rollout starts with `maxghenis`.
- Favor `private` or `unlisted` defaults until redaction is stronger.

## Notes

- These are worth turning into real GitHub issues once the whole product has a single top-level repo.
- Until then, tracking them here is less confusing than filing architecture issues inside the nested `web/` repo.
