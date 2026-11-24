#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
CATALOG_KAFKA_TEST_IMAGE=$(sh "$root/tests/kafka-test-image.sh" "${KAFKA_TEST_VERSION:-4.3.2}")
export CATALOG_KAFKA_TEST_IMAGE
work=$(mktemp -d)
project="catalogkafka-$(basename "$work" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')"
host_owner="$(id -u):$(id -g)"
base() {
    docker compose --env-file /dev/null --project-directory "$root" -p "$project" \
        -f "$root/tests/catalog-kafka-compose.yml" "$@"
}
secure() {
    base -f "$root/deploy/compose.catalog-kafka-tls.yml" "$@"
}
feed_tls() {
    secure -f "$root/deploy/compose.feed-kafka-tls.yml" "$@"
}
feed_rollback() {
    secure -f "$root/deploy/compose.feed-kafka-rollback.yml" "$@"
}
sandbox_tls() {
    feed_tls -f "$root/deploy/compose.sandbox-kafka-tls.yml" "$@"
}
sandbox_rollback() {
    feed_tls -f "$root/deploy/compose.sandbox-kafka-rollback.yml" "$@"
}
content_tls() {
    feed_tls -f "$root/deploy/compose.content-kafka-tls.yml" "$@"
}
content_rollback() {
    feed_tls -f "$root/deploy/compose.content-kafka-rollback.yml" "$@"
}
fixture_chown() {
    docker run --rm --network none --user 0:0 --entrypoint chown \
        --mount "type=bind,src=$work,dst=/fixture" "$CATALOG_KAFKA_TEST_IMAGE" "$@"
}
cleanup() {
    base down >/dev/null 2>&1 || :
    if [ "$host_owner" != 1000:1000 ] && [ -d "$work/data" ]; then
        fixture_chown -R "$host_owner" /fixture/data >/dev/null
    fi
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM

umask 077
export RELEASE_CATALOG_KAFKA_TLS_DIR="$work/pki"
export CATALOG_KAFKA_TEST_DATA="$work/data"
mkdir -m 755 "$RELEASE_CATALOG_KAFKA_TLS_DIR" "$CATALOG_KAFKA_TEST_DATA"
OVM_KAFKA_CATALOG_BROKER_PASSWORD=$(openssl rand -hex 32)
OVM_KAFKA_CATALOG_ADMIN_PASSWORD=$(openssl rand -hex 32)
OVM_KAFKA_CATALOG_ENTITIES_PASSWORD=$(openssl rand -hex 32)
OVM_KAFKA_FEED_PASSWORD=$(openssl rand -hex 32)
export OVM_KAFKA_CATALOG_BROKER_PASSWORD OVM_KAFKA_CATALOG_ADMIN_PASSWORD OVM_KAFKA_CATALOG_ENTITIES_PASSWORD
export OVM_KAFKA_FEED_PASSWORD

make_ca() {
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 1 \
        -subj "/CN=$1" -addext 'basicConstraints=critical,CA:TRUE' \
        -keyout "$work/$1.key" -out "$work/$1.pem" 2>/dev/null
}
make_ca kafka-ca
make_ca wrong-ca
openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj /CN=kafka \
    -keyout "$work/broker.key" -out "$work/broker.csr" 2>/dev/null
printf '%s\n' 'subjectAltName=DNS:kafka' 'extendedKeyUsage=serverAuth' > "$work/ext.cnf"
openssl x509 -req -in "$work/broker.csr" -CA "$work/kafka-ca.pem" -CAkey "$work/kafka-ca.key" \
    -CAcreateserial -days 1 -extfile "$work/ext.cnf" -out "$work/broker-cert.pem" 2>/dev/null
cat "$work/broker.key" "$work/broker-cert.pem" "$work/kafka-ca.pem" > "$RELEASE_CATALOG_KAFKA_TLS_DIR/broker.pem"
cp "$work/kafka-ca.pem" "$RELEASE_CATALOG_KAFKA_TLS_DIR/ca.pem"
chmod 640 "$RELEASE_CATALOG_KAFKA_TLS_DIR/broker.pem"
chmod 644 "$RELEASE_CATALOG_KAFKA_TLS_DIR/ca.pem"
if [ "$host_owner" != 1000:1000 ]; then
    fixture_chown 1000:1000 /fixture/data /fixture/pki/broker.pem
fi

wait_kafka() {
    step=0
    until base exec -T kafka timeout 8 /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 \
        --list >/dev/null 2>&1; do
        step=$((step + 1))
        if [ "$step" -eq 10 ]; then
            printf '%s\n' 'Kafka test listener did not become ready' >&2
            exit 1
        fi
        sleep 1
    done
}
produce() {
    bootstrap=$1
    topic=$2
    shift 2
    base exec -T kafka /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server "$bootstrap" \
        --topic "$topic" --sync --reader-property parse.key=true --reader-property 'key.separator=|' \
        --command-property max.block.ms=8000 --command-property delivery.timeout.ms=10000 "$@"
}
read_catalog() {
    base exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 \
        --topic catalog.activity.v1 --group feed-v1 --from-beginning --max-messages 2 --timeout-ms 8000
}
read_one() {
    bootstrap=$1
    topic=$2
    group=$3
    shift 3
    base exec -T kafka /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server "$bootstrap" \
        --topic "$topic" --group "$group" --from-beginning --max-messages 1 --timeout-ms 8000 "$@"
}
expect_denied() {
    error=$1
    shift
    "$@" > "$work/denied" 2>&1 || :
    if ! grep -Fq "$error" "$work/denied"; then
        printf 'Expected Kafka rejection: %s for %s %s %s\n' "$error" "$1" "$2" "$3" >&2
        exit 1
    fi
}
save_client() {
    base exec -T kafka sh -c 'umask 077; cat > "/tmp/$1"' sh "$1"
}
properties() {
    mechanism=${5:-PLAIN}
    login=plain.PlainLoginModule
    if [ "$mechanism" = SCRAM-SHA-256 ]; then login=scram.ScramLoginModule; fi
    {
        printf '%s\n' 'security.protocol=SASL_SSL' "sasl.mechanism=$mechanism" 'ssl.truststore.type=PEM'
        printf 'ssl.truststore.location=/tmp/%s\n' "$3"
        printf 'sasl.jaas.config=org.apache.kafka.common.security.%s required username="%s" password="%s";\n' "$login" "$1" "$2"
    } | save_client "$4"
}

sandbox_rollout() {
    requests=code-execution.requests.v1
    results=code-execution.results.v1
    retry=code-execution.retry.v1
    worker_group=sandbox-code-execution-v1
    tasks_group=tasks-code-results-v1
    export RELEASE_SANDBOX_KAFKA_DIR="$work/worker"
    mkdir -m 755 "$RELEASE_SANDBOX_KAFKA_DIR"
    worker_password=$(openssl rand -hex 32)
    printf '%s\n' sandbox > "$RELEASE_SANDBOX_KAFKA_DIR/username"
    printf '%s\n' "$worker_password" > "$RELEASE_SANDBOX_KAFKA_DIR/password"
    chmod 640 "$RELEASE_SANDBOX_KAFKA_DIR/username" "$RELEASE_SANDBOX_KAFKA_DIR/password"
    fixture_chown 10001:10001 /fixture/worker/username /fixture/worker/password

    feed_tls up -d kafka
    wait_kafka
    feed_tls run --rm -T --no-deps kafka-topics
    printf '%s\n' 'job|consumed-before-tls' | produce kafka:9092 "$requests"
    [ "$(read_one kafka:9092 "$requests" "$worker_group")" = consumed-before-tls ]
    printf '%s\n' 'job|queued-before-tls' | produce kafka:9092 "$requests"

    sandbox_tls up -d kafka
    wait_kafka
    sandbox_tls run --rm -T --no-deps kafka-topics
    save_client ca.pem < "$work/kafka-ca.pem"
    properties sandbox "$worker_password" ca.pem sandbox.properties SCRAM-SHA-256
    properties sandbox "$(openssl rand -hex 32)" ca.pem wrong-scram.properties SCRAM-SHA-256
    [ "$(read_one kafka:9094 "$requests" "$worker_group" --command-config /tmp/sandbox.properties)" = queued-before-tls ]
    printf '%s\n' 'job|result-after-tls' | produce kafka:9094 "$results" --command-config /tmp/sandbox.properties
    [ "$(read_one kafka:9092 "$results" "$tasks_group")" = result-after-tls ]
    printf '%s\n' 'job|retry-after-tls' | produce kafka:9094 "$retry" --command-config /tmp/sandbox.properties
    [ "$(read_one kafka:9094 "$retry" "$worker_group" --command-config /tmp/sandbox.properties)" = retry-after-tls ]
    expect_denied SaslAuthenticationException read_one kafka:9094 "$requests" "$worker_group" \
        --command-config /tmp/wrong-scram.properties
    expect_denied GroupAuthorizationException read_one kafka:9092 "$results" "$worker_group"
    expect_denied TopicAuthorizationException read_one kafka:9092 "$requests" "$tasks_group"
    printf '%s\n' 'job|legacy-cannot-write-result' | expect_denied TopicAuthorizationException \
        produce kafka:9092 "$results" --command-property enable.idempotence=false
    printf '%s\n' 'job|worker-cannot-write-request' | expect_denied TopicAuthorizationException \
        produce kafka:9094 "$requests" --command-config /tmp/sandbox.properties --command-property enable.idempotence=false
    printf '%s\n' 'job|queued-for-rollback' | produce kafka:9092 "$requests"
    properties entities "$OVM_KAFKA_CATALOG_ENTITIES_PASSWORD" ca.pem entities.properties
    properties feed "$OVM_KAFKA_FEED_PASSWORD" ca.pem feed.properties
    printf '%s\n' 'catalog|feed-still-uses-tls' | produce kafka:9094 catalog.activity.v1 --command-config /tmp/entities.properties
    [ "$(read_one kafka:9094 catalog.activity.v1 feed-v1 --command-config /tmp/feed.properties)" = feed-still-uses-tls ]

    sandbox_rollback up -d kafka
    wait_kafka
    sandbox_rollback run --rm -T --no-deps kafka-topics
    [ "$(read_one kafka:9092 "$requests" "$worker_group")" = queued-for-rollback ]
    save_client ca.pem < "$work/kafka-ca.pem"
    properties catalog_admin "$OVM_KAFKA_CATALOG_ADMIN_PASSWORD" ca.pem admin.properties
    credentials=$(base exec -T kafka /opt/kafka/bin/kafka-configs.sh --bootstrap-server kafka:9094 \
        --command-config /tmp/admin.properties --describe --entity-type users --entity-name sandbox)
    if printf '%s\n' "$credentials" | grep -Fq 'SCRAM-SHA-256='; then
        printf '%s\n' 'Sandbox rollback left a SCRAM credential' >&2
        exit 1
    fi
    printf '%s\n' 'Sandbox SCRAM, task requests, results and offsets survived rollout and rollback'
}

content_rollout() {
    material=content.material.v1
    learning=content.learning.v1
    recommendations_group=recommendations-learning-evidence-v1
    OVM_KAFKA_CONTENT_PASSWORD=$(openssl rand -hex 32)
    export OVM_KAFKA_CONTENT_PASSWORD

    feed_tls up -d kafka
    wait_kafka
    feed_tls run --rm -T --no-deps kafka-topics
    save_client ca.pem < "$work/kafka-ca.pem"
    properties feed "$OVM_KAFKA_FEED_PASSWORD" ca.pem feed.properties
    printf '%s\n' 'material|before-tls' | produce kafka:9092 "$material"
    [ "$(read_one kafka:9094 "$material" feed-v1 --command-config /tmp/feed.properties)" = before-tls ]
    [ "$(read_one kafka:9092 "$material" "$recommendations_group")" = before-tls ]

    content_tls up -d kafka
    wait_kafka
    content_tls run --rm -T --no-deps kafka-topics
    save_client ca.pem < "$work/kafka-ca.pem"
    properties content "$OVM_KAFKA_CONTENT_PASSWORD" ca.pem content.properties
    properties feed "$OVM_KAFKA_FEED_PASSWORD" ca.pem feed.properties
    printf '%s\n' 'material|after-tls' | produce kafka:9094 "$material" --command-config /tmp/content.properties
    [ "$(read_one kafka:9094 "$material" feed-v1 --command-config /tmp/feed.properties)" = after-tls ]
    [ "$(read_one kafka:9092 "$material" "$recommendations_group")" = after-tls ]
    printf '%s\n' 'material|learning-evidence' | produce kafka:9094 "$learning" --command-config /tmp/content.properties
    [ "$(read_one kafka:9092 "$learning" "$recommendations_group")" = learning-evidence ]
    printf '%s\n' 'material|anonymous-write' | expect_denied TopicAuthorizationException \
        produce kafka:9092 "$material" --command-property enable.idempotence=false
    printf '%s\n' 'material|foreign-write' | expect_denied TopicAuthorizationException \
        produce kafka:9094 "$material" --command-config /tmp/feed.properties --command-property enable.idempotence=false
    printf '%s\n' 'material|other-topic' | expect_denied TopicAuthorizationException \
        produce kafka:9094 catalog.activity.v1 --command-config /tmp/content.properties \
        --command-property enable.idempotence=false

    content_rollback up -d kafka
    wait_kafka
    content_rollback run --rm -T --no-deps kafka-topics
    save_client ca.pem < "$work/kafka-ca.pem"
    properties content "$OVM_KAFKA_CONTENT_PASSWORD" ca.pem content.properties
    properties feed "$OVM_KAFKA_FEED_PASSWORD" ca.pem feed.properties
    printf '%s\n' 'material|revoked-account' | expect_denied SaslAuthenticationException \
        produce kafka:9094 "$material" --command-config /tmp/content.properties
    printf '%s\n' 'material|after-rollback' | produce kafka:9092 "$material"
    [ "$(read_one kafka:9094 "$material" feed-v1 --command-config /tmp/feed.properties)" = after-rollback ]
    [ "$(read_one kafka:9092 "$material" "$recommendations_group")" = after-rollback ]
    printf '%s\n' 'Content TLS, consumer offsets and the explicit rollback passed'
}

case "${1:-}" in
    sandbox) sandbox_rollout; exit ;;
    content) content_rollout; exit ;;
    '') ;;
    *) exit 2 ;;
esac

base up -d kafka
wait_kafka
base run --rm -T --no-deps kafka-topics
printf '%s\n' 'catalog|before-upgrade' | produce kafka:9092 catalog.activity.v1

secure up -d kafka
wait_kafka
secure run --rm -T --no-deps kafka-topics
save_client ca.pem < "$work/kafka-ca.pem"
save_client wrong-ca.pem < "$work/wrong-ca.pem"
properties entities "$OVM_KAFKA_CATALOG_ENTITIES_PASSWORD" ca.pem entities.properties
properties entities "$OVM_KAFKA_CATALOG_ENTITIES_PASSWORD" wrong-ca.pem wrong-ca.properties
properties entities "$(openssl rand -hex 32)" ca.pem wrong-password.properties

printf '%s\n' 'catalog|after-upgrade' | produce kafka:9094 catalog.activity.v1 --command-config /tmp/entities.properties
records=$(read_catalog)
[ "$records" = "$(printf '%s\n' before-upgrade after-upgrade)" ]
printf '%s\n' 'catalog|plain-cannot-write' | expect_denied TopicAuthorizationException produce kafka:9092 catalog.activity.v1
printf '%s\n' 'goal|plain-cannot-write' | expect_denied TopicAuthorizationException produce kafka:9092 learning.goal.v1
printf '%s\n' 'task|legacy-still-writes' | produce kafka:9092 code-execution.requests.v1
printf '%s\n' 'task|entities-cannot-write' | expect_denied TopicAuthorizationException produce kafka:9094 \
    code-execution.requests.v1 --command-config /tmp/entities.properties
printf '%s\n' 'catalog|wrong-ca' | expect_denied SSLHandshakeException produce kafka:9094 \
    catalog.activity.v1 --command-config /tmp/wrong-ca.properties
printf '%s\n' 'catalog|wrong-password' | expect_denied SaslAuthenticationException produce kafka:9094 \
    catalog.activity.v1 --command-config /tmp/wrong-password.properties
printf '%s\n' 'Catalog TLS, preserved records and Kafka ACLs passed'

properties catalog_admin "$OVM_KAFKA_CATALOG_ADMIN_PASSWORD" ca.pem admin.properties
offsets=$(base exec -T kafka /opt/kafka/bin/kafka-consumer-groups.sh --bootstrap-server kafka:9094 \
    --command-config /tmp/admin.properties --describe --group feed-v1)
printf '%s\n' "$offsets" | awk '$1 == "feed-v1" && $2 == "catalog.activity.v1" && $4 == 2 { found=1 } END { exit !found }'

feed_tls up -d kafka
wait_kafka
feed_tls run --rm -T --no-deps kafka-topics
save_client ca.pem < "$work/kafka-ca.pem"
properties feed "$OVM_KAFKA_FEED_PASSWORD" ca.pem feed.properties
properties entities "$OVM_KAFKA_CATALOG_ENTITIES_PASSWORD" ca.pem entities.properties
printf '%s\n' 'catalog|feed-after-tls' | produce kafka:9094 catalog.activity.v1 --command-config /tmp/entities.properties
[ "$(read_one kafka:9094 catalog.activity.v1 feed-v1 --command-config /tmp/feed.properties)" = feed-after-tls ]
printf '%s\n' 'catalog|feed-cannot-write' | expect_denied TopicAuthorizationException produce kafka:9094 \
    catalog.activity.v1 --command-config /tmp/feed.properties --command-property enable.idempotence=false
expect_denied TopicAuthorizationException read_one kafka:9094 learning.goal.v1 feed-v1 \
    --command-config /tmp/feed.properties
expect_denied GroupAuthorizationException read_one kafka:9092 content.material.v1 feed-v1
expect_denied TopicAuthorizationException read_one kafka:9092 catalog.activity.v1 recommendations-learning-evidence-v1
printf '%s\n' 'material|legacy-recommendations' | produce kafka:9092 content.material.v1
[ "$(read_one kafka:9092 content.material.v1 recommendations-learning-evidence-v1)" = legacy-recommendations ]

# An ordinary catalog provisioning run must not silently reopen old Feed access.
secure run --rm -T --no-deps kafka-topics
expect_denied GroupAuthorizationException read_one kafka:9092 content.material.v1 feed-v1

feed_rollback up -d kafka
wait_kafka
feed_rollback run --rm -T --no-deps kafka-topics
save_client ca.pem < "$work/kafka-ca.pem"
properties entities "$OVM_KAFKA_CATALOG_ENTITIES_PASSWORD" ca.pem entities.properties
printf '%s\n' 'catalog|feed-after-rollback' | produce kafka:9094 catalog.activity.v1 --command-config /tmp/entities.properties
[ "$(read_one kafka:9092 catalog.activity.v1 feed-v1)" = feed-after-rollback ]
printf '%s\n' 'Feed TLS and explicit rollback preserved consumer offsets'
