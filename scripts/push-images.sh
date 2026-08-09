#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

: "${IMAGE_REGISTRY:?IMAGE_REGISTRY is required}"
TAG="${IMAGE_TAG:-latest}"

docker push "${IMAGE_REGISTRY}/users:${TAG}"
docker push "${IMAGE_REGISTRY}/entities:${TAG}"
docker push "${IMAGE_REGISTRY}/tasks-it:${TAG}"
docker push "${IMAGE_REGISTRY}/api-gateway:${TAG}"
docker push "${IMAGE_REGISTRY}/frontend:${TAG}"
