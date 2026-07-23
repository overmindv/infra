#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

: "${IMAGE_REGISTRY:?IMAGE_REGISTRY is required}"
TAG="${IMAGE_TAG:-latest}"

docker push "${IMAGE_REGISTRY}/arcee:${TAG}"
docker push "${IMAGE_REGISTRY}/ironhide:${TAG}"
docker push "${IMAGE_REGISTRY}/laserbeak:${TAG}"
docker push "${IMAGE_REGISTRY}/soundwave:${TAG}"
