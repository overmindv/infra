#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"

compose_config() {
    docker compose --env-file .env.example --env-file tests/monitoring-images.env \
        -f docker-compose.yml -f compose.observability.yml config "$@"
}

compose_config --quiet
prometheus_image=$(compose_config --images prometheus)
alertmanager_image=$(compose_config --images alertmanager)

docker run --rm --network none --entrypoint /bin/promtool \
    -v "$root/observability:/observability:ro" -v "$root/tests:/tests:ro" \
    "$prometheus_image" check config --syntax-only /observability/prometheus/prometheus.yml
docker run --rm --network none --entrypoint /bin/promtool \
    -v "$root/observability:/observability:ro" -v "$root/tests:/tests:ro" \
    "$prometheus_image" test rules /tests/monitoring-rules.yml
docker run --rm --network none --entrypoint /bin/amtool \
    -v "$root/observability:/observability:ro" \
    "$alertmanager_image" check-config /observability/alertmanager.yml

sh -n observability/signals.sh
sh -n observability/uptime.sh
sh tests/monitoring-scripts.sh
