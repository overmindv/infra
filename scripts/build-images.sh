#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

REGISTRY="${IMAGE_REGISTRY:-overmindv}"
TAG="${IMAGE_TAG:-local}"

docker build -t "${REGISTRY}/users:${TAG}" ../users
docker build -t "${REGISTRY}/entities:${TAG}" ../entities
docker build -t "${REGISTRY}/tasks-it:${TAG}" ../tasks-it
docker build -t "${REGISTRY}/api-gateway:${TAG}" ../api-gateway
docker build -t "${REGISTRY}/frontend:${TAG}" ../frontend
