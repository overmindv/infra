#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"

: "${RELEASE_BUILD:?RELEASE_BUILD must point to the build receipt}"
: "${RELEASE_MANIFEST:?RELEASE_MANIFEST must be a new output file}"

exec "${PYTHON:-python3}" "$ROOT/scripts/release_manifest.py" publish \
  --build "$RELEASE_BUILD" --output "$RELEASE_MANIFEST"

