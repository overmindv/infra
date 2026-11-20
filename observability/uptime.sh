#!/bin/sh
set -eu

if [ "$#" -ne 2 ]; then
    printf '%s\n' 'uptime.sh https://public-host/health /path/to/ping-url' >&2
    exit 2
fi

ping=$(cat -- "$2")
# Keep the ping URL out of process arguments, errors and curl config syntax.
case "$ping" in
    https://*) ;;
    *) exit 2 ;;
esac
case "$ping" in
    *[!A-Za-z0-9_.:/-]*|*/) exit 2 ;;
esac

probe_ok=0
status=$(curl -q --silent --connect-timeout 3 --max-time 5 --noproxy '*' \
    --proto '=https' --output /dev/null --write-out '%{http_code}' -- "$1") || status=000
case "$status" in
    2[0-9][0-9]) probe_ok=1 ;;
    *) ping="$ping/fail" ;;
esac

status=$(printf 'url = "%s"\n' "$ping" | curl -q --silent --connect-timeout 3 --max-time 5 --noproxy '*' \
    --proto '=https' --output /dev/null --write-out '%{http_code}' --config -) || status=000
case "$status" in
    2[0-9][0-9]) ;;
    *) printf '%s\n' 'Uptime receiver did not accept the ping' >&2; exit 1 ;;
esac

[ "$probe_ok" -eq 1 ] || {
    printf '%s\n' 'Public health probe failed' >&2
    exit 1
}
