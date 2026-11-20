#!/bin/sh
set -eu
export LC_ALL=C

usage() {
    printf '%s\n' 'signals.sh <signals-dir> disk <data-path> | <signals-dir> backup <ok|failed>' >&2
    exit 2
}

[ "$#" -eq 3 ] || usage
directory=$1
[ -d "$directory" ] || usage
umask 077
stamp=$(date +%s)

write_metrics() {
    target=$1
    shift
    tmp=$(mktemp "$directory/.metrics.XXXXXXXX")
    trap 'rm -f -- "$tmp"' EXIT
    trap 'exit 1' HUP INT TERM
    printf '%s\n' "$@" > "$tmp"
    chmod 0644 "$tmp"
    mv -fT -- "$tmp" "$directory/$target.prom"
    trap - EXIT HUP INT TERM
}

case "$2:$3" in
    disk:*)
        # Measure the filesystem that owns the data, not the container root.
        disk=$(df -Pk -- "$3")
        ratio=$(printf '%s\n' "$disk" | awk '
            NR == 2 && $2 > 0 && $4 >= 0 && $4 <= $2 {
                printf "%.8f\n", $4 / $2
            }
        ')
        [ -n "$ratio" ] || exit 1
        write_metrics disk \
            "# TYPE overmind_disk_available_ratio gauge" \
            "overmind_disk_available_ratio $ratio" \
            "# TYPE overmind_disk_sample_timestamp_seconds gauge" \
            "overmind_disk_sample_timestamp_seconds $stamp"
        ;;
    backup:ok|backup:failed)
        ok=0
        if [ "$3" = ok ]; then
            write_metrics backup-success \
                "# TYPE overmind_backup_last_success_timestamp_seconds gauge" \
                "overmind_backup_last_success_timestamp_seconds $stamp"
            ok=1
        fi
        write_metrics backup-attempt \
            "# TYPE overmind_backup_last_attempt_timestamp_seconds gauge" \
            "overmind_backup_last_attempt_timestamp_seconds $stamp" \
            "# TYPE overmind_backup_last_attempt_ok gauge" \
            "overmind_backup_last_attempt_ok $ok"
        ;;
    *)
        usage
        ;;
esac
