#!/bin/sh
set -eu

script=./scripts/kafka/topics.sh
plan=$(sh "$script" plan)
for topic in code-execution.requests.v1 code-execution.results.v1 code-execution.retry.v1 \
    code-execution.events.v1 code-execution.deadletter.v1 content.content-item-events \
    catalog.activity.v1 task.activity.v1 learning.outcome.v1 learning.goal.v1 content.material.v1 \
    content.learning.v1 content.contribution.v1 user.deleted.v1; do
    printf '%s\n' "$plan" | awk -v topic="$topic" '$1 == topic { count++ } END { exit count != 1 }'
done
printf '%s\n' "$plan" | grep -Fxq 'user.deleted.v1 262144 - - -'
printf '%s\n' "$plan" | grep -Fxq 'content.material.v1 262144 content feed feed-v1'
printf '%s\n' "$plan" | grep -Fxq 'learning.goal.v1 262144 recommendations - -'
if printf '%s\n' "$plan" | grep -q 'code-execution\..*\.v2'; then
    exit 1
fi
plan=$(KAFKA_RESERVE_EXECUTION_V2=true KAFKA_REQUESTS_TOPIC=exercise.requests.v1 sh "$script" plan)
printf '%s\n' "$plan" | grep -Fxq 'exercise.requests.v1 3145728 tasks sandbox sandbox-code-execution-v1'
printf '%s\n' "$plan" | grep -Fxq 'code-execution.requests.v2 3145728 tasks-v2 sandbox-v2 sandbox-code-execution-v2'

reject() {
    if env "$@" sh "$script" plan >/dev/null 2>&1; then
        printf 'Invalid Kafka plan was accepted\n' >&2
        exit 1
    fi
}
reject KAFKA_RESULTS_TOPIC=learning.outcome.v1
reject KAFKA_CONTENT_LEARNING_TOPIC=learning.goal.v1
reject KAFKA_RESULTS_TOPIC=code-execution_requests_v1
reject KAFKA_REQUESTS_TOPIC=--alter
reject 'KAFKA_REQUESTS_TOPIC= leading-space'
reject KAFKA_REQUESTS_TOPIC=__consumer_offsets
reject KAFKA_RESULTS_CONSUMER_GROUP=sandbox-code-execution-v1
reject FEED_CONSUMER_GROUP=sandbox-code-execution-v1
reject KAFKA_REQUESTS_TOPIC=code-execution.requests.v2
reject SANDBOX_KAFKA_GROUP_ID=sandbox-code-execution-v2
reject KAFKA_RESERVE_EXECUTION_V2=yes
reject OVM_KAFKA_TASKS_TLS=true
reject OVM_KAFKA_TASKS_TLS=rollback
reject OVM_KAFKA_TASKS_TLS=yes
reject KAFKA_RETENTION_MS=-1
reject KAFKA_EXECUTION_MAX_BATCH_BYTES=8192
reject KAFKA_METADATA_MAX_BATCH_BYTES=2147483647
