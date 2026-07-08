#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

BASE_URL="${LASERBEAK_URL:-http://localhost:8081}"

attempt=0
until curl -fsS "${BASE_URL}/health" >/dev/null; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "laserbeak did not become healthy" >&2
    exit 1
  fi
  sleep 2
done

suffix="$(date +%s)$$"
email="integration-${suffix}@example.com"
username="integration_${suffix}"

register_payload="{\"query\":\"mutation { register(input: {email: \\\"${email}\\\", password: \\\"password\\\", username: \\\"${username}\\\"}) { token user { id email username } } }\"}"
register_response="$(curl -fsS "${BASE_URL}/graphql" -H 'Content-Type: application/json' --data "$register_payload")"

REGISTER_RESPONSE="$register_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["REGISTER_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["register"]["token"], payload
assert payload["data"]["register"]["user"]["id"], payload
PY

login_payload="{\"query\":\"mutation { login(input: {email: \\\"${email}\\\", password: \\\"password\\\"}) { token user { id email } } }\"}"
login_response="$(curl -fsS "${BASE_URL}/graphql" -H 'Content-Type: application/json' --data "$login_payload")"

LOGIN_RESPONSE="$login_response" python3 - <<'PY'
import json, os
payload = json.loads(os.environ["LOGIN_RESPONSE"])
assert not payload.get("errors"), payload
assert payload["data"]["login"]["token"], payload
PY

echo "integration registration/login: OK"
