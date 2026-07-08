# Ratchet infrastructure

Ratchet contains no business logic. It owns local orchestration and the Kubernetes baseline for the Overmindv repositories stored as siblings.

Arcee and Laserbeak communicate exclusively through `http://arcee:8080/query`. Laserbeak is the backend entry point on host port `8081`; Soundwave is served on `http://localhost:3000` and proxies `/graphql` to Laserbeak. Future `bumblebee`, `optimus-prime` and `mirage` placeholders are behind the Compose profile `future` and do not consume resources by default.

## Docker Compose

```bash
cd ratchet
cp .env.example .env
# Replace POSTGRES_PASSWORD and JWT_SECRET in .env.
docker compose up -d --build
# equivalent: make up
```

Startup order is health-gated:

1. PostgreSQL becomes ready.
2. The Arcee image runs embedded goose migrations.
3. Arcee becomes DB-healthy.
4. Laserbeak becomes Arcee-healthy.
5. Soundwave starts.

Check the stack:

```bash
curl http://localhost:8081/health
open http://localhost:3000
open http://localhost:8081/playground
make integration
```

Stop and remove the local database volume with `make down`.

## Kubernetes (kind or minikube)

Prerequisites: Docker, `kubectl`, and either [kind](https://kind.sigs.k8s.io/) or [minikube](https://minikube.sigs.k8s.io/docs/). For Ingress, install/enable an NGINX ingress controller (`minikube addons enable ingress` on minikube).

Kind example:

```bash
kind create cluster --name overmindv
export POSTGRES_PASSWORD='local-db-password'
export JWT_SECRET='local-long-random-secret'
export KIND_CLUSTER=overmindv
make deploy-k8s
```

For minikube, start it and run the same deployment command. The script builds Arcee, Laserbeak and Soundwave images, loads them into the detected local cluster, creates `overmindv-secrets` from environment variables, applies manifests, and waits for rollouts.

Add `127.0.0.1 overmindv.local` to `/etc/hosts` when the ingress controller is locally reachable, or use:

```bash
kubectl -n overmindv port-forward service/laserbeak 8081:8081
```

`k8s/secret.yaml` documents required keys but is deliberately excluded from Kustomize. Never commit real secrets; `scripts/deploy-k8s.sh` creates the Secret from environment variables.

## Commands

```bash
make up           # complete Compose stack
make down         # stop and delete volumes
make build        # build Arcee/Laserbeak/Soundwave images
make push         # push images to IMAGE_REGISTRY
make test         # validate Compose and run backend/frontend tests
make integration  # registration and login through the gateway
make render-k8s   # render manifests
make deploy-k8s   # local kind/minikube deployment
```
