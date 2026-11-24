#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
broker=
cleanup() {
    if [ -n "$broker" ]; then docker stop "$broker" >/dev/null 2>&1 || :; fi
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

case "$#:${1:-}" in
    0:) set -- 4.3.1 4.3.2 ;;
    1:sha256:*) set -- 4.3.2 "$1" ;;
    *) printf '%s\n' 'Usage: kafka-release-perf.sh [local candidate image ID]' >&2; exit 2 ;;
esac

for version; do
    image=$(sh "$root/kafka-test-image.sh" "$version")
    broker=$(docker run -d --rm --pull never --platform linux/amd64 --network none \
        --hostname kafka-bench --add-host kafka-bench:127.0.0.1 \
        --cpus 2 --memory 1g --tmpfs /tmp/kafka-bench:mode=1777,size=268435456 \
        --env CLUSTER_ID=MkU3OEVBNTcwNTJENDM2Qk \
        --env KAFKA_NODE_ID=1 --env KAFKA_PROCESS_ROLES=broker,controller \
        --env KAFKA_LISTENER_SECURITY_PROTOCOL_MAP=PLAINTEXT:PLAINTEXT,CONTROLLER:PLAINTEXT \
        --env KAFKA_LISTENERS=PLAINTEXT://127.0.0.1:9092,CONTROLLER://127.0.0.1:9093 \
        --env KAFKA_ADVERTISED_LISTENERS=PLAINTEXT://127.0.0.1:9092 \
        --env KAFKA_CONTROLLER_LISTENER_NAMES=CONTROLLER --env KAFKA_INTER_BROKER_LISTENER_NAME=PLAINTEXT \
        --env KAFKA_CONTROLLER_QUORUM_VOTERS=1@127.0.0.1:9093 \
        --env KAFKA_AUTO_CREATE_TOPICS_ENABLE=false --env KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR=1 \
        --env 'KAFKA_HEAP_OPTS=-Xms128m -Xmx384m' --env KAFKA_LOG_DIRS=/tmp/kafka-bench \
        "$image")
    step=0
    until ready=$(docker exec "$broker" timeout 15 /opt/kafka/bin/kafka-topics.sh \
        --bootstrap-server 127.0.0.1:9092 --list 2>&1); do
        step=$((step + 1))
        if [ "$step" -eq 6 ]; then
            printf 'Kafka %s benchmark broker did not become ready\n' "$version" >&2
            printf '%s\n' "$ready" >&2
            exit 1
        fi
        sleep 1
    done
    printf 'Kafka %s ready, %s\n' "$version" "$image"
    for run in 1 2 3; do
        topic="perf-$run"
        docker exec "$broker" timeout 30 /opt/kafka/bin/kafka-topics.sh \
            --bootstrap-server 127.0.0.1:9092 --create --topic "$topic" \
            --partitions 1 --replication-factor 1
        printf 'Kafka %s run %s: 50,000 records, 5,000 warmup, 512 bytes\n' "$version" "$run"
        stats=$(docker exec "$broker" timeout 45 /opt/kafka/bin/kafka-producer-perf-test.sh \
            --bootstrap-server 127.0.0.1:9092 --topic "$topic" \
            --num-records 50000 --warmup-records 5000 --record-size 512 --throughput -1 \
            --command-property acks=all linger.ms=5 compression.type=none)
        printf '%s\n' "$stats"
        printf '%s\n' "$stats" | grep -q '^50000 records sent,'
        printf '%s\n' "$stats" | grep -q '^45000 steady state records sent,'
    done
    docker stop "$broker" >/dev/null
    broker=
done
