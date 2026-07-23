#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

REGISTRY="${IMAGE_REGISTRY:-overmindv}"
TAG="${IMAGE_TAG:-local}"

docker build -t "${REGISTRY}/arcee:${TAG}" ../arcee
docker build -t "${REGISTRY}/ironhide:${TAG}" ../ironhide
docker build -t "${REGISTRY}/laserbeak:${TAG}" ../laserbeak
docker build -t "${REGISTRY}/soundwave:${TAG}" ../soundwave
