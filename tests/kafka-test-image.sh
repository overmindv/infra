#!/bin/sh
set -eu

case "${1:-4.3.2}" in
    4.3.1)
        printf '%s\n' 'apache/kafka:4.3.1@sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837'
        ;;
    4.3.2)
        printf '%s\n' 'apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7'
        ;;
    sha256:*)
        if [ "${#1}" -ne 71 ] || ! printf '%s\n' "$1" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
            printf '%s\n' 'Expected a full Kafka fixture image ID' >&2
            exit 2
        fi
        # Docker returns the child manifest ID when a platform is selected.
        actual=$(docker image inspect --format '{{.Id}}' "$1")
        platform=$(docker image inspect --platform linux/amd64 \
            --format '{{.Os}}/{{.Architecture}} {{.Config.User}}' "$1")
        if [ "$actual" != "$1" ] || [ "$platform" != 'linux/amd64 appuser' ]; then
            printf '%s\n' 'Expected a local linux/amd64 Kafka image running as appuser' >&2
            exit 2
        fi
        printf '%s\n' "$1"
        ;;
    *)
        printf '%s\n' 'Expected Kafka version 4.3.1, 4.3.2 or a local image ID' >&2
        exit 2
        ;;
esac
