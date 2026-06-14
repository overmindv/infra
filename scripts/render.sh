#!/usr/bin/env sh
set -eu

ENVIRONMENT="${1:-dev}"

kubectl kustomize "k8s/${ENVIRONMENT}"
