#!/usr/bin/env bash
# logpile.sh — run Logpile without activating the venv manually.
# Usage: ./logpile.sh [command] [options]
#   logpile.sh sync [-v]
#   logpile.sh serve [--port 5002] [--public]
#   logpile.sh serve --dev           # Next.js dev mode with HMR
#   logpile.sh private <session-id>
#
# Symlink to ~/bin/logpile for global access:
#   ln -sf ~/logpile/logpile.sh ~/bin/logpile

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
WEB_DIR="$SCRIPT_DIR/web"

# Bootstrap Python venv
if [[ ! -x "$VENV/bin/logpile" ]]; then
  echo "Bootstrapping Python…"
  cd "$SCRIPT_DIR"
  uv venv --python 3.11 "$VENV" >/dev/null 2>&1 || uv venv "$VENV" >/dev/null
  uv pip install -e . --quiet
  echo "Done."
fi

# Bootstrap bun deps for serve command
if [[ "${1:-}" == "serve" ]] && [[ ! -d "$WEB_DIR/node_modules" ]]; then
  echo "Installing Next.js dependencies…"
  (cd "$WEB_DIR" && bun install --silent)
fi

exec "$VENV/bin/logpile" "$@"
