# Logpile Web

This is the Next.js product surface for Logpile.

It is not a standalone app. By default it reads the SQLite database and shared
session directory from the parent checkout:

- `../logpile.db`
- `../shared/`

Override those paths with:

- `LOGPILE_DB_PATH`
- `LOGPILE_SHARED_DIR`
- `LOGPILE_PYTHON_BIN`
- `LOGPILE_PUBLIC_MODE=true`

## Local development

From the product root:

```bash
logpile serve --dev
```

Or run the app directly:

```bash
cd web
bun dev
```

## Scope

The Next app owns the main product UI:

- dashboard
- sessions
- repos
- people and profiles
- publish queue and review UX

The Python package still owns ingestion, parsing, sync, deterministic
enrichment, redaction, and publish review logic.
