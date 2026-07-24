#!/usr/bin/env bash
# Deploy site/index.html to the `logpile-landing` Cloudflare Worker serving
# https://logpile.ai. Rebuild first: python3 scripts/build_landing.py
#
# Auth: CLOUDFLARE_EMAIL + CLOUDFLARE_GLOBAL_API_KEY env vars, falling back to
# the `agent-secret` keychain helper. The zone token cannot manage Workers.
set -euo pipefail
umask 077

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HTML="$ROOT/site/index.html"
NAME="logpile-landing"
HOSTNAME="logpile.ai"
API="https://api.cloudflare.com/client/v4"

CURL_CONFIG=""
WORKER=""
VERIFY_BODY=""
cleanup() {
  [[ -z "$CURL_CONFIG" ]] || rm -f -- "$CURL_CONFIG"
  [[ -z "$WORKER" ]] || rm -f -- "$WORKER"
  [[ -z "$VERIFY_BODY" ]] || rm -f -- "$VERIFY_BODY"
}
trap cleanup EXIT

[[ -f "$HTML" ]] || { echo "missing $HTML — run scripts/build_landing.py first" >&2; exit 1; }

EMAIL="${CLOUDFLARE_EMAIL:-$(agent-secret get CLOUDFLARE_EMAIL)}"
KEY="${CLOUDFLARE_GLOBAL_API_KEY:-$(agent-secret get CLOUDFLARE_GLOBAL_API_KEY)}"

case "$EMAIL" in
  *$'\n'*|*$'\r'*) echo "invalid newline in CLOUDFLARE_EMAIL" >&2; exit 1 ;;
esac
case "$KEY" in
  *$'\n'*|*$'\r'*) echo "invalid newline in CLOUDFLARE_GLOBAL_API_KEY" >&2; exit 1 ;;
esac

curl_config_escape() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "$value"
}

# Keep the Global API Key out of curl's argv. The config is mode 0600 (via the
# restrictive umask plus chmod) and is deleted by the EXIT trap.
CURL_CONFIG="$(mktemp "${TMPDIR:-/tmp}/logpile-curl-config.XXXXXX")"
chmod 600 "$CURL_CONFIG"
printf 'header = "X-Auth-Email: %s"\nheader = "X-Auth-Key: %s"\n' \
  "$(curl_config_escape "$EMAIL")" "$(curl_config_escape "$KEY")" >"$CURL_CONFIG"

curl_api() {
  curl --config "$CURL_CONFIG" --silent --show-error --fail-with-body "$@"
}

cf_success() {
  local label="$1"
  python3 -c '
import json
import sys

label = sys.argv[1]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError) as error:
    print(f"{label}: invalid Cloudflare response: {error}", file=sys.stderr)
    raise SystemExit(1)
if payload.get("success") is not True:
    print(f"{label}: Cloudflare API failed: {payload.get('"'"'errors'"'"', payload)}", file=sys.stderr)
    raise SystemExit(1)
print(f"{label}: ok")
' "$label"
}

cf_single_id() {
  local label="$1"
  local override_name="$2"
  python3 -c '
import json
import sys

label, override_name = sys.argv[1:]
try:
    payload = json.load(sys.stdin)
except (json.JSONDecodeError, OSError) as error:
    print(f"{label}: invalid Cloudflare response: {error}", file=sys.stderr)
    raise SystemExit(1)
if payload.get("success") is not True:
    print(f"{label}: Cloudflare API failed: {payload.get('"'"'errors'"'"', payload)}", file=sys.stderr)
    raise SystemExit(1)
results = payload.get("result")
if not isinstance(results, list) or len(results) != 1 or not results[0].get("id"):
    count = len(results) if isinstance(results, list) else 0
    print(f"{label}: expected exactly one result, found {count}; set {override_name}", file=sys.stderr)
    raise SystemExit(1)
print(results[0]["id"])
' "$label" "$override_name"
}

if [[ -n "${CLOUDFLARE_ACCOUNT_ID:-}" ]]; then
  ACCOUNT_ID="$CLOUDFLARE_ACCOUNT_ID"
else
  ACCOUNT_ID="$(curl_api "$API/accounts" | cf_single_id "account lookup" "CLOUDFLARE_ACCOUNT_ID")"
fi

if [[ -n "${CLOUDFLARE_ZONE_ID:-}" ]]; then
  ZONE_ID="$CLOUDFLARE_ZONE_ID"
else
  ZONE_ID="$(curl_api "$API/zones?name=$HOSTNAME" | cf_single_id "zone lookup" "CLOUDFLARE_ZONE_ID")"
fi

WORKER=$(mktemp "${TMPDIR:-/tmp}/logpile-worker.XXXXXX")
python3 - "$HTML" >"$WORKER" <<'PY'
import json, sys
html = open(sys.argv[1]).read()
print("const HTML = " + json.dumps(html) + ";")
print('export default { async fetch() { return new Response(HTML, { headers: {'
      ' "content-type": "text/html; charset=utf-8",'
      ' "cache-control": "public, max-age=300" } }); } };')
PY

curl_api -X PUT "$API/accounts/$ACCOUNT_ID/workers/scripts/$NAME" \
  -F 'metadata={"main_module":"worker.js"};type=application/json' \
  -F "worker.js=@$WORKER;type=application/javascript+module;filename=worker.js" \
  | cf_success "worker upload"

# Idempotent: attach the worker to the apex custom domain.
curl_api -X PUT "$API/accounts/$ACCOUNT_ID/workers/domains" \
  -H "Content-Type: application/json" \
  -d "{\"zone_id\":\"$ZONE_ID\",\"hostname\":\"$HOSTNAME\",\"service\":\"$NAME\",\"environment\":\"production\"}" \
  | cf_success "domain attach"

# Fetch the public endpoint and compare bytes with the source page. A cache
# buster and short retry cover propagation without allowing a stale deployment
# to be reported as successful.
LOCAL_HASH="$(python3 - "$HTML" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
VERIFY_BODY="$(mktemp "${TMPDIR:-/tmp}/logpile-deploy-verify.XXXXXX")"
REMOTE_HASH=""
for attempt in 1 2 3; do
  if curl --silent --show-error --fail-with-body \
      --output "$VERIFY_BODY" "https://$HOSTNAME/?logpile-deploy-verify=$LOCAL_HASH"; then
    REMOTE_HASH="$(python3 - "$VERIFY_BODY" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
    [[ "$REMOTE_HASH" != "$LOCAL_HASH" ]] || break
  fi
  [[ "$attempt" == 3 ]] || sleep 1
done

if [[ "$REMOTE_HASH" != "$LOCAL_HASH" ]]; then
  echo "deployment verification failed: expected sha256 $LOCAL_HASH, got ${REMOTE_HASH:-no response}" >&2
  exit 1
fi

echo "verified https://$HOSTNAME (sha256 $LOCAL_HASH)"
echo "deployed https://$HOSTNAME"
