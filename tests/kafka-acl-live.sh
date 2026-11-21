#!/bin/sh
set -eu

root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
fixture=${TASKS_FIXTURES_DIR:-../tasks/tests/fixtures/execution-contract}
[ -r "$fixture/execution.request.json" ] && [ -r "$fixture/execution.result.json" ] || {
    printf 'Set TASKS_FIXTURES_DIR to the Tasks execution contract fixtures\n' >&2
    exit 2
}

work=$(mktemp -d)
project="kafkaacl-$(basename "$work" | tr '[:upper:]' '[:lower:]' | tr -cd '[:alnum:]')"
export FIXTURE_KAFKA_PASSWORD="$project"
compose() {
    docker compose -f tests/kafka-acl-compose.yml -p "$project" "$@"
}
cleanup() {
    compose down >/dev/null 2>&1 || :
    rm -rf -- "$work"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
compose up -d broker

properties() {
    principal=$1
    {
        printf '%s\n' 'security.protocol=SASL_PLAINTEXT' 'sasl.mechanism=PLAIN' 'request.timeout.ms=6000' 'default.api.timeout.ms=15000'
        printf 'sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="%s" password="%s";\n' "$principal" "$FIXTURE_KAFKA_PASSWORD"
    } | compose exec -T broker sh -c 'umask 077; cat > "/tmp/$1.properties"' sh "$principal"
}
for principal in fixture_admin tasks sandbox tasks-v2 sandbox-v2 feed content users recommendations; do
    properties "$principal"
done

# Wait on the authenticated listener, not just the container process.
step=0
until compose exec -T broker /opt/kafka/bin/kafka-topics.sh --bootstrap-server broker:9092 \
    --command-config /tmp/fixture_admin.properties --list >/dev/null 2>&1; do
    step=$((step + 1))
    if [ "$step" -ge 10 ]; then
        printf 'Test broker did not become ready\n' >&2
        exit 1
    fi
    sleep 1
done

provision() {
    compose exec -T -e "KAFKA_RESERVE_EXECUTION_V2=$1" broker \
        sh /opt/overmind/kafka/topics.sh secure broker:9092 /tmp/fixture_admin.properties
}
provision false

produce() {
    principal=$1
    topic=$2
    shift 2
    compose exec -T broker /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server broker:9092 \
        --command-config "/tmp/$principal.properties" --topic "$topic" --sync \
        --reader-property parse.key=true --reader-property 'key.separator=|' \
        --command-property max.block.ms=5000 --command-property delivery.timeout.ms=8000 \
        --command-property compression.type=none "$@"
}

consume() {
    principal=$1
    topic=$2
    group=$3
    compose exec -T broker /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server broker:9092 \
        --command-config "/tmp/$principal.properties" --topic "$topic" --group "$group" \
        --from-beginning --max-messages 1 --timeout-ms 6000 --formatter-property print.key=true \
        --formatter-property 'key.separator=|'
}

expect_record() {
    wanted=$1
    shift
    raw=$(consume "$@") || return 1
    actual=$(printf '%s\n' "$raw" | tr -d '\r' | tail -n 1)
    [ "$actual" = "$wanted" ] || {
        printf 'Unexpected Kafka record on %s\n' "$2" >&2
        exit 1
    }
}

expect_denied() {
    error=$1
    shift
    "$@" > "$work/denied" 2>&1 || :
    if ! grep -Fq "$error" "$work/denied"; then
        printf 'Expected Kafka denial: %s\n' "$error" >&2
        exit 1
    fi
}

key=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["submission_id"])' "$fixture/execution.request.json")
record() {
    printf '%s|' "$key"
    tr -d '\r\n' < "$fixture/execution.$1.json"
    printf '\n'
}
# Leave a v1 record queued before the v2 topics exist.
request=$(record request)
result=$(record result)
printf '%s\n' "$request" | produce tasks code-execution.requests.v1
provision true
printf '%s\n' "${key}|v2 fixture" | produce tasks-v2 code-execution.requests.v2
expect_record "${key}|v2 fixture" sandbox-v2 code-execution.requests.v2 sandbox-code-execution-v2
expect_record "$request" sandbox code-execution.requests.v1 sandbox-code-execution-v1
printf '%s\n' "$result" | produce sandbox code-execution.results.v1
expect_record "$result" tasks code-execution.results.v1 tasks-code-results-v1

printf '%s\n' "$request" | expect_denied TopicAuthorizationException produce feed code-execution.requests.v1 \
    --command-property enable.idempotence=false
printf '%s\n' "$result" | expect_denied TopicAuthorizationException produce sandbox code-execution.results.v2
printf '%s\n' "${key}|reserved" | expect_denied TopicAuthorizationException produce users user.deleted.v1 \
    --command-property enable.idempotence=false
expect_denied GroupAuthorizationException consume sandbox code-execution.requests.v1 tasks-code-results-v1
expect_denied TopicAuthorizationException consume sandbox code-execution.results.v1 sandbox-code-execution-v1

material="${key}|published-material-fixture"
printf '%s\n' "$material" | produce content content.material.v1
expect_record "$material" feed content.material.v1 feed-v1
printf '%s\n' "$material" | expect_denied TopicAuthorizationException produce feed content.material.v1 \
    --command-property enable.idempotence=false
expect_denied TopicAuthorizationException consume feed content.learning.v1 feed-v1
learning="${key}|self-reported-reading-fixture"
printf '%s\n' "$learning" | produce content content.learning.v1
expect_record "$learning" recommendations content.learning.v1 recommendations-learning-evidence-v1
editorial="${key}|accepted-editorial-fixture"
printf '%s\n' "$editorial" | produce content content.contribution.v1
expect_denied TopicAuthorizationException consume feed content.contribution.v1 feed-v1
expect_denied TopicAuthorizationException consume recommendations content.contribution.v1 recommendations-learning-evidence-v1
{
    printf '%s|' "$key"
    head -c 300000 /dev/zero | tr '\000' 'a'
    printf '\n'
} | expect_denied RecordTooLargeException produce tasks learning.outcome.v1

if compose exec -T -e KAFKA_RESERVE_EXECUTION_V2=true -e KAFKA_RETENTION_MS=604800001 broker \
    sh /opt/overmind/kafka/topics.sh secure broker:9092 /tmp/fixture_admin.properties > "$work/drift" 2>&1; then
    printf 'Provisioning accepted retention drift\n' >&2
    exit 1
fi
grep -Fq 'configuration drift on code-execution.requests.v1' "$work/drift"
compose exec -T broker /opt/kafka/bin/kafka-topics.sh --bootstrap-server broker:9092 \
    --command-config /tmp/fixture_admin.properties --describe --topic code-execution.requests.v1 |
    grep -Eq '(^|[ ,])retention\.ms=604800000(,|$)'

printf '%s\n' 'Kafka topic limits, ACLs and parallel lanes passed'
