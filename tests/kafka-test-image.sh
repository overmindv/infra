#!/bin/sh
set -eu

case "${1:-4.3.2}" in
    4.3.1)
        printf '%s\n' 'apache/kafka:4.3.1@sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837'
        ;;
    4.3.2)
        printf '%s\n' 'apache/kafka:4.3.2@sha256:852a0d49fc4e656b022e7f23785411802c9a088c78a7c5e39f33969de9c227a7'
        ;;
    *)
        printf '%s\n' 'Kafka fixture version must be 4.3.1 or 4.3.2' >&2
        exit 2
        ;;
esac
