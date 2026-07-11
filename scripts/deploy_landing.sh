#!/usr/bin/env bash
# Deploy site/index.html to the `logpile-landing` Cloudflare Worker serving
# https://logpile.ai. Rebuild first: python3 scripts/build_landing.py
#
# Auth: CLOUDFLARE_EMAIL + CLOUDFLARE_GLOBAL_API_KEY env vars, falling back to
# the `agent-secret` keychain helper. The zone token cannot manage Workers.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HTML="$ROOT/site/index.html"
NAME="logpile-landing"
HOSTNAME="logpile.ai"

EMAIL="${CLOUDFLARE_EMAIL:-$(agent-secret get CLOUDFLARE_EMAIL)}"
KEY="${CLOUDFLARE_GLOBAL_API_KEY:-$(agent-secret get CLOUDFLARE_GLOBAL_API_KEY)}"
AUTH=(-H "X-Auth-Email: $EMAIL" -H "X-Auth-Key: $KEY")
API="https://api.cloudflare.com/client/v4"

[[ -f "$HTML" ]] || { echo "missing $HTML — run scripts/build_landing.py first" >&2; exit 1; }

ACCOUNT_ID=$(curl -s "${AUTH[@]}" "$API/accounts" | python3 -c "import json,sys; print(json.load(sys.stdin)['result'][0]['id'])")
ZONE_ID=$(curl -s "${AUTH[@]}" "$API/zones?name=$HOSTNAME" | python3 -c "import json,sys; print(json.load(sys.stdin)['result'][0]['id'])")

WORKER=$(mktemp -t logpile-worker)
python3 - "$HTML" >"$WORKER" <<'PY'
import json, sys
html = open(sys.argv[1]).read()
print("const HTML = " + json.dumps(html) + ";")
print('export default { async fetch() { return new Response(HTML, { headers: {'
      ' "content-type": "text/html; charset=utf-8",'
      ' "cache-control": "public, max-age=300" } }); } };')
PY

curl -s -X PUT "$API/accounts/$ACCOUNT_ID/workers/scripts/$NAME" "${AUTH[@]}" \
  -F 'metadata={"main_module":"worker.js"};type=application/json' \
  -F "worker.js=@$WORKER;type=application/javascript+module;filename=worker.js" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('worker upload:', 'ok' if d['success'] else d['errors'])"

# Idempotent: attach the worker to the apex custom domain.
curl -s -X PUT "$API/accounts/$ACCOUNT_ID/workers/domains" "${AUTH[@]}" \
  -H "Content-Type: application/json" \
  -d "{\"zone_id\":\"$ZONE_ID\",\"hostname\":\"$HOSTNAME\",\"service\":\"$NAME\",\"environment\":\"production\"}" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); print('domain attach:', 'ok' if d['success'] else d['errors'])"

rm -f "$WORKER"
echo "deployed https://$HOSTNAME"
