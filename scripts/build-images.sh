#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

. "$ROOT/scripts/lib.sh"

for svc in $SERVICES; do
  docker build -t "${REGISTRY}/${svc}:${LOCAL_TAG}" "../$svc"
done
