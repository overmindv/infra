#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
state=$(mktemp -d)
project="monitorcheck-$(basename "$state" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')"
export MONITORING_STATE_DIR="$state" MONITORING_UI_PORT=0

compose() {
    docker compose --env-file .env.example --env-file tests/monitoring-images.env -p "$project" \
        -f docker-compose.yml -f compose.observability.yml -f tests/monitoring-compose.yml "$@"
}

cleanup() {
    compose down >/dev/null 2>&1 || :
    rm -rf -- "$state"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

mkdir "$state/alertmanager" "$state/prometheus" "$state/signals" "$state/targets"
cp observability/targets/sandbox.example.yml "$state/targets/sandbox.yml"
printf '%s\n' 'http://monitor-test-sink:8080/alerts' > "$state/alertmanager/webhook-url"
printf '%s\n' "$project" > "$state/alertmanager/webhook-token"
# Fixture credentials must not be reused outside this test.
password=$project
hash=$(perl -e 'print crypt($ARGV[0], q{$2y$05$abcdefghijklmnopqrstuu})' "$password")
case "$hash" in
    '$2'*) ;;
    *) printf '%s\n' 'Perl crypt with bcrypt support is required' >&2; exit 1 ;;
esac
printf 'basic_auth_users:\n  fixture: %s\n' "$hash" > "$state/prometheus/web.yml"
sh observability/signals.sh "$state/signals" disk "$state"
sh observability/signals.sh "$state/signals" backup ok

compose up -d --no-build prometheus alertmanager monitoring-signals \
    monitor-test-sink monitor-test-tasks monitor-test-worker
endpoint="http://$(compose port prometheus 9090)"

query() {
    curl -fsS --max-time 3 -u "fixture:$password" -G "$endpoint/api/v1/query" --data-urlencode "query=$1"
}

expect_value() {
    expr=$1
    value=$2
    tries=0
    while [ "$tries" -lt 45 ]; do
        if out=$(query "$expr" 2>/dev/null) && printf '%s' "$out" | python3 -c '
import json, sys
result = json.load(sys.stdin)["data"]["result"]
sys.exit(not (len(result) == 1 and result[0]["value"][1] == sys.argv[1]))
' "$value"; then
            return
        fi
        tries=$((tries + 1))
        sleep 1
    done
    printf 'Unexpected metrics for %s\n' "$expr" >&2
    exit 1
}

expect_value 'up{job="sandbox"}' 1
expect_value 'count(parker_http_requests_total{instance="tasks:8080"})' 1
expect_value 'sum(parker_http_requests_total{instance="tasks:8080"})' 42
expect_value 'overmind_backup_last_attempt_ok' 1
code=$(curl -sS --max-time 3 -o /dev/null -w '%{http_code}' "$endpoint/api/v1/query?query=up")
test "$code" = 401

compose stop monitor-test-worker
sh observability/signals.sh "$state/signals" backup failed
expect_value 'overmind_backup_last_attempt_ok' 0

tries=0
while [ "$tries" -lt 48 ]; do
    if compose exec -T monitor-test-sink python -c '
import json, sys, urllib.request
with urllib.request.urlopen("http://127.0.0.1:8080/delivered", timeout=2) as response:
    alerts = json.load(response)
want = [
    ["firing", "MonitoringTargetDown", "sandbox"],
    ["firing", "BackupFailed", "host-signals"],
]
sys.exit(not all(alert in alerts for alert in want))
'; then
        printf '%s\n' 'Worker outage and backup failure reached the test receiver'
        exit 0
    fi
    tries=$((tries + 1))
    sleep 5
done

printf '%s\n' 'Test receiver did not get both alerts' >&2
exit 1
