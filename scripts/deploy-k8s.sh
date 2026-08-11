#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
cd "$ROOT"

: "${USERS_POSTGRES_PASSWORD:?USERS_POSTGRES_PASSWORD is required}"
: "${ENTITIES_POSTGRES_PASSWORD:?ENTITIES_POSTGRES_PASSWORD is required}"
: "${TASKS_IT_POSTGRES_PASSWORD:?TASKS_IT_POSTGRES_PASSWORD is required}"
: "${TASK_HUNTER_POSTGRES_PASSWORD:?TASK_HUNTER_POSTGRES_PASSWORD is required}"
: "${TASK_HUNTER_INGEST_TOKEN:?TASK_HUNTER_INGEST_TOKEN is required}"
: "${TASK_HUNTER_GATEWAY_TOKEN:?TASK_HUNTER_GATEWAY_TOKEN is required}"
: "${TASK_HUNTER_TELEGRAM_API_ID:?TASK_HUNTER_TELEGRAM_API_ID is required}"
: "${TASK_HUNTER_TELEGRAM_API_HASH:?TASK_HUNTER_TELEGRAM_API_HASH is required}"
: "${JWT_SECRET:?JWT_SECRET is required}"
: "${BOOTSTRAP_SUPERUSER_EMAIL:?BOOTSTRAP_SUPERUSER_EMAIL is required}"
: "${BOOTSTRAP_SUPERUSER_PASSWORD:?BOOTSTRAP_SUPERUSER_PASSWORD is required}"

REGISTRY="${IMAGE_REGISTRY:-overmindv}"
TAG="${IMAGE_TAG:-local}"
NAMESPACE="${K8S_NAMESPACE:-overmindv}"

IMAGE_REGISTRY="$REGISTRY" IMAGE_TAG="$TAG" ./scripts/build-images.sh

# Локальный кластер получает собранные образы без внешнего registry.
if command -v kind >/dev/null 2>&1 && kind get clusters | grep -qx "${KIND_CLUSTER:-overmindv}"; then
  kind load docker-image --name "${KIND_CLUSTER:-overmindv}" \
    "${REGISTRY}/users:${TAG}" \
    "${REGISTRY}/entities:${TAG}" \
    "${REGISTRY}/tasks-it:${TAG}" \
    "${REGISTRY}/task-hunter:${TAG}" \
    "${REGISTRY}/api-gateway:${TAG}" \
    "${REGISTRY}/frontend:${TAG}"
elif command -v minikube >/dev/null 2>&1 && minikube status >/dev/null 2>&1; then
  minikube image load "${REGISTRY}/users:${TAG}"
  minikube image load "${REGISTRY}/entities:${TAG}"
  minikube image load "${REGISTRY}/tasks-it:${TAG}"
  minikube image load "${REGISTRY}/task-hunter:${TAG}"
  minikube image load "${REGISTRY}/api-gateway:${TAG}"
  minikube image load "${REGISTRY}/frontend:${TAG}"
fi

kubectl apply -f k8s/namespace.yaml

# Secret создаётся отдельно, чтобы секреты не попадали в git.
kubectl -n "$NAMESPACE" create secret generic overmindv-secrets \
  --from-literal=USERS_POSTGRES_PASSWORD="$USERS_POSTGRES_PASSWORD" \
  --from-literal=USERS_DATABASE_URL="postgres://postgres:${USERS_POSTGRES_PASSWORD}@users-postgres:5432/users?sslmode=disable" \
  --from-literal=ENTITIES_POSTGRES_PASSWORD="$ENTITIES_POSTGRES_PASSWORD" \
  --from-literal=ENTITIES_DATABASE_URL="postgres://entities:${ENTITIES_POSTGRES_PASSWORD}@entities-postgres:5432/entities?sslmode=disable" \
  --from-literal=TASKS_IT_POSTGRES_PASSWORD="$TASKS_IT_POSTGRES_PASSWORD" \
  --from-literal=TASKS_IT_DATABASE_URL="postgres://tasks_it:${TASKS_IT_POSTGRES_PASSWORD}@tasks-it-postgres:5432/tasks_it?sslmode=disable" \
  --from-literal=TASK_HUNTER_POSTGRES_PASSWORD="$TASK_HUNTER_POSTGRES_PASSWORD" \
  --from-literal=TASK_HUNTER_DATABASE_URL="postgres://task_hunter:${TASK_HUNTER_POSTGRES_PASSWORD}@task-hunter-postgres:5432/task_hunter?sslmode=disable" \
  --from-literal=TASK_HUNTER_INGEST_TOKEN="$TASK_HUNTER_INGEST_TOKEN" \
  --from-literal=TASK_HUNTER_GATEWAY_TOKEN="$TASK_HUNTER_GATEWAY_TOKEN" \
  --from-literal=TASK_HUNTER_TELEGRAM_API_ID="$TASK_HUNTER_TELEGRAM_API_ID" \
  --from-literal=TASK_HUNTER_TELEGRAM_API_HASH="$TASK_HUNTER_TELEGRAM_API_HASH" \
  --from-literal=JWT_SECRET="$JWT_SECRET" \
  --from-literal=BOOTSTRAP_SUPERUSER_EMAIL="$BOOTSTRAP_SUPERUSER_EMAIL" \
  --from-literal=BOOTSTRAP_SUPERUSER_PASSWORD="$BOOTSTRAP_SUPERUSER_PASSWORD" \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl apply -k k8s
kubectl -n "$NAMESPACE" set image deployment/users users="${REGISTRY}/users:${TAG}" users-migrate="${REGISTRY}/users:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/entities entities="${REGISTRY}/entities:${TAG}" entities-migrate="${REGISTRY}/entities:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/tasks-it tasks-it="${REGISTRY}/tasks-it:${TAG}" tasks-it-migrate="${REGISTRY}/tasks-it:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/task-hunter task-hunter="${REGISTRY}/task-hunter:${TAG}" task-hunter-migrate="${REGISTRY}/task-hunter:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/api-gateway api-gateway="${REGISTRY}/api-gateway:${TAG}"
kubectl -n "$NAMESPACE" set image deployment/frontend frontend="${REGISTRY}/frontend:${TAG}"

for deployment in \
  users-postgres \
  entities-postgres \
  tasks-it-postgres \
  task-hunter-postgres \
  users \
  entities \
  tasks-it \
  task-hunter \
  api-gateway \
  frontend; do
  kubectl -n "$NAMESPACE" rollout status "deployment/$deployment" --timeout=180s
done
