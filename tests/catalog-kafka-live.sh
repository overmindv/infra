#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
work=$(mktemp -d)
project="catalogkafka-$(basename "$work" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')"
base() {
    docker compose --env-file /dev/null --project-directory "$root" -p "$project" \
        -f "$root/tests/catalog-kafka-compose.yml" "$@"
}
secure() {
    base -f "$root/deploy/compose.catalog-kafka-tls.yml" "$@"
}
cleanup() {
    base down >/dev/null 2>&1 || :
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
export OVM_KAFKA_CATALOG_BROKER_PASSWORD OVM_KAFKA_CATALOG_ADMIN_PASSWORD OVM_KAFKA_CATALOG_ENTITIES_PASSWORD

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
expect_denied() {
    error=$1
    shift
    "$@" > "$work/denied" 2>&1 || :
    if ! grep -Fq "$error" "$work/denied"; then
        printf 'Expected Kafka rejection: %s\n' "$error" >&2
        exit 1
    fi
}
save_client() {
    base exec -T kafka sh -c 'umask 077; cat > "/tmp/$1"' sh "$1"
}
properties() {
    {
        printf '%s\n' 'security.protocol=SASL_SSL' 'sasl.mechanism=PLAIN' 'ssl.truststore.type=PEM'
        printf 'ssl.truststore.location=/tmp/%s\n' "$3"
        printf 'sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="%s" password="%s";\n' "$1" "$2"
    } | save_client "$4"
}

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
