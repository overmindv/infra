#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
trap 'exit 1' HUP INT TERM

sh "$root/observability/signals.sh" "$tmp" disk "$tmp"
grep -Eq '^overmind_disk_available_ratio (0\.[0-9]+|1\.0+)$' "$tmp/disk.prom"
grep -Eq '^overmind_disk_sample_timestamp_seconds [0-9]+$' "$tmp/disk.prom"

sh "$root/observability/signals.sh" "$tmp" backup failed
test ! -e "$tmp/backup-success.prom"
grep -qx 'overmind_backup_last_attempt_ok 0' "$tmp/backup-attempt.prom"

sh "$root/observability/signals.sh" "$tmp" backup ok
grep -qx 'overmind_backup_last_attempt_ok 1' "$tmp/backup-attempt.prom"
confirmed=$(cat "$tmp/backup-success.prom")
sh "$root/observability/signals.sh" "$tmp" backup failed
grep -qx 'overmind_backup_last_attempt_ok 0' "$tmp/backup-attempt.prom"
test "$(cat "$tmp/backup-success.prom")" = "$confirmed"

if sh "$root/observability/signals.sh" "$tmp" disk "$tmp/missing" 2>/dev/null; then
    exit 1
fi

mkdir "$tmp/bin"
cat > "$tmp/bin/curl" <<'CURL'
#!/bin/sh
set -eu
case "$*" in
    *--config*)
        cat > "$CAPTURE"
        printf '%s' "${PING_STATUS:-204}"
        ;;
    *https://service.example/health)
        printf '%s' "${HEALTH_STATUS:-200}"
        ;;
    *) exit 1 ;;
esac
CURL
chmod +x "$tmp/bin/curl"
export PATH="$tmp/bin:$PATH" CAPTURE="$tmp/captured"
printf '%s\n' 'https://ping.example/check' > "$tmp/ping"
sh "$root/observability/uptime.sh" https://service.example/health "$tmp/ping"
grep -Fxq 'url = "https://ping.example/check"' "$CAPTURE"
if HEALTH_STATUS=503 sh "$root/observability/uptime.sh" https://service.example/health "$tmp/ping"; then
    exit 1
fi
grep -Fxq 'url = "https://ping.example/check/fail"' "$CAPTURE"
if HEALTH_STATUS=302 sh "$root/observability/uptime.sh" https://service.example/health "$tmp/ping"; then
    exit 1
fi
if PING_STATUS=500 sh "$root/observability/uptime.sh" https://service.example/health "$tmp/ping"; then
    exit 1
fi
printf 'https://ping.example/check"\nurl = "http://other.example\n' > "$tmp/ping"
if sh "$root/observability/uptime.sh" https://service.example/health "$tmp/ping"; then
    exit 1
fi
