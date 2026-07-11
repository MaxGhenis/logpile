# Logpile Web

Next.js 16 + Tailwind 4 + better-sqlite3. This is the product surface for Logpile, not a standalone app — it reads the sibling SQLite database populated by the Python `logpile sync` CLI.

## Running

The canonical way is from the product root, which handles the `bun install` bootstrap and env-var wiring:

```bash
./logpile.sh serve             # production build, port 5002
./logpile.sh serve --dev       # Next.js dev server with HMR
./logpile.sh serve --public    # public read-only mode
```

Direct `bun dev` works too if the env vars are set:

```bash
LOGPILE_DB_PATH=../logpile.db \
LOGPILE_SHARED_DIR=../shared \
bun dev
```

## Env vars

| Var | Default | Purpose |
|---|---|---|
| `LOGPILE_DB_PATH` | `../logpile.db` | SQLite database path |
| `LOGPILE_SHARED_DIR` | `../shared` | Shared JSONL transcripts directory |
| `LOGPILE_PUBLIC_MODE` | `false` | `true` enables hosted read-only mode |
| `LOGPILE_PYTHON_BIN` | auto-detect from `../.venv` | Python used for publish review scanning |

## Scope

This app owns dashboard, sessions, repos, people, profiles, publish queue, and review UX. The Python package (`../logpile/`) owns ingestion, parsing, deterministic enrichment, visibility rules, and the publish-review secret/PII scanner.

## This is NOT the Next.js you know

Next 16 has breaking changes from earlier versions. When writing code here, consult `node_modules/next/dist/docs/` rather than older training data. See `AGENTS.md`.
