#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

: "${POSTGRES_PASSWORD:?POSTGRES_PASSWORD is required}"
: "${JWT_SECRET:?JWT_SECRET is required}"

REGISTRY="${IMAGE_REGISTRY:-overmindv}"
TAG="${IMAGE_TAG:-local}"
NAMESPACE="${K8S_NAMESPACE:-overmindv}"

IMAGE_REGISTRY="$REGISTRY" IMAGE_TAG="$TAG" ./scripts/build-images.sh

if command -v kind >/dev/null 2>&1 && kind get clusters | grep -qx "${KIND_CLUSTER:-overmindv}"; then
  kind load docker-image --name "${KIND_CLUSTER:-overmindv}" "${REGISTRY}/arcee:${TAG}" "${REGISTRY}/laserbeak:${TAG}" "${REGISTRY}/soundwave:${TAG}"
elif command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
  minikube image load "${REGISTRY}/arcee:${TAG}"
  minikube image load "${REGISTRY}/laserbeak:${TAG}"
  minikube image load "${REGISTRY}/soundwave:${TAG}"
fi

kubectl apply -f k8s/namespace.yaml
kubectl -n "$NAMESPACE" create secret generic overmindv-secrets \
  --from-literal=POSTGRES_DB="${POSTGRES_DB:-arcee}" \
  --from-literal=POSTGRES_USER="${POSTGRES_USER:-postgres}" \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  --from-literal=DATABASE_URL="postgres://${POSTGRES_USER:-postgres}:${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB:-arcee}?sslmode=disable" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -k k8s
kubectl -n "$NAMESPACE" set image deployment/arcee arcee="${REGISTRY}/arcee:${TAG}" arcee-migrate="${REGISTRY}/arcee:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/laserbeak laserbeak="${REGISTRY}/laserbeak:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/soundwave soundwave="${REGISTRY}/soundwave:${TAG}"
kubectl -n "$NAMESPACE" rollout status deployment/arcee --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/laserbeak --timeout=180s
kubectl -n "$NAMESPACE" rollout status deployment/soundwave --timeout=180s
