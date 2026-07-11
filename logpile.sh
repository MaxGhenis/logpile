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

# Follow symlink chains before locating the checkout. This keeps an invocation
# such as ~/bin/logpile working even when that link points outside the repo.
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
  SOURCE_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE_LINK="$(readlink "$SOURCE")"
  if [[ "$SOURCE_LINK" == /* ]]; then
    SOURCE="$SOURCE_LINK"
  else
    SOURCE="$SOURCE_DIR/$SOURCE_LINK"
  fi
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
WEB_DIR="$SCRIPT_DIR/web"

require_command() {
  local name="$1"
  local install_url="$2"

  if ! command -v "$name" >/dev/null 2>&1; then
    echo "logpile: required command '$name' was not found." >&2
    echo "Install it from $install_url and retry." >&2
    exit 127
  fi
}

# Bootstrap Python venv
if [[ ! -x "$VENV/bin/logpile" ]]; then
  require_command uv "https://docs.astral.sh/uv/getting-started/installation/"
  echo "Bootstrapping Python…"
  cd "$SCRIPT_DIR"
  UV_FROZEN=false uv sync --locked --quiet
  echo "Done."
fi

# Bootstrap bun deps for the Next.js serve command.
needs_bun=false
if [[ "${1:-}" == "serve" ]]; then
  needs_bun=true
  for arg in "$@"; do
    if [[ "$arg" == "--flask" ]]; then
      needs_bun=false
    fi
  done
fi

if [[ "$needs_bun" == true ]]; then
  require_command bun "https://bun.sh/docs/installation"
  if [[ ! -d "$WEB_DIR/node_modules" ]]; then
    echo "Installing Next.js dependencies…"
  fi
  # Always reconcile node_modules with the lockfile. Presence alone does not
  # prove that a checkout has picked up patched dependency versions.
  (cd "$WEB_DIR" && bun install --frozen-lockfile --silent)
fi

exec "$VENV/bin/logpile" "$@"
