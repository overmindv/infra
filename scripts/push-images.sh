#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

. "$ROOT/scripts/lib.sh"

: "${IMAGE_REGISTRY:?IMAGE_REGISTRY is required}"

for svc in $SERVICES; do
  docker push "${IMAGE_REGISTRY}/${svc}:${LATEST_TAG}"
done

