#!/bin/sh
set -eu
export LC_ALL=C

die() {
    printf 'Kafka topics: %s\n' "$1" >&2
    exit 1
}

mode=${1:-plan}
bootstrap=${2:-kafka:9092}
admin_config=${3:-}
case "$mode:$#" in
    plan:0|plan:1|local:1|local:2) ;;
    secure:3)
        [ -n "$2" ] || die 'bootstrap servers are required'
        [ -f "$admin_config" ] && [ -r "$admin_config" ] || die 'admin properties file is not readable'
        ;;
    *) die 'usage: topics.sh plan | local [bootstrap] | secure bootstrap admin.properties' ;;
esac

partitions=${KAFKA_TOPIC_PARTITIONS:-3}
replicas=${KAFKA_TOPIC_REPLICATION_FACTOR:-1}
retention=${KAFKA_RETENTION_MS:-604800000}
execution_max=${KAFKA_EXECUTION_MAX_BATCH_BYTES:-3145728}
metadata_max=${KAFKA_METADATA_MAX_BATCH_BYTES:-262144}
legacy_max=1048588
v2=${KAFKA_RESERVE_EXECUTION_V2:-false}
tasks_group=${KAFKA_RESULTS_CONSUMER_GROUP:-tasks-code-results-v1}
sandbox_group=${SANDBOX_KAFKA_GROUP_ID:-sandbox-code-execution-v1}
feed_group=${FEED_CONSUMER_GROUP:-feed-v1}
material_topic=${KAFKA_CONTENT_MATERIAL_TOPIC:-content.material.v1}

number() {
    case "$2" in
        ''|0*|*[!0-9]*) die "invalid $1" ;;
    esac
    [ "${#2}" -le 10 ] && [ "$2" -le "$3" ] || die "invalid $1"
}

number partitions "$partitions" 128
number replicas "$replicas" 9
number retention "$retention" 2592000000
number execution_max "$execution_max" 10485760
number metadata_max "$metadata_max" 1048576
[ "$execution_max" -ge "$legacy_max" ] || die 'execution limit below the legacy batch limit'
[ "$metadata_max" -ge 4096 ] || die 'metadata limit below 4 KiB'
case "$v2" in true|false) ;; *) die 'KAFKA_RESERVE_EXECUTION_V2 must be true or false' ;; esac

name() {
    case "$1" in
        ''|[!A-Za-z0-9]*|*[!A-Za-z0-9._-]*) die 'invalid topic or group name' ;;
    esac
    [ "${#1}" -le 249 ] || die 'topic or group name is too long'
}

row() {
    name "$1"
    [ "$5" = - ] || name "$5"
    printf '%s %s %s %s %s\n' "$@"
}

# Each row names the sole writer and the current reader, if there is one.
catalog() {
    row "${KAFKA_REQUESTS_TOPIC:-code-execution.requests.v1}" "$execution_max" tasks sandbox "$sandbox_group"
    row "${KAFKA_RESULTS_TOPIC:-code-execution.results.v1}" "$execution_max" sandbox tasks "$tasks_group"
    row "${KAFKA_RETRY_TOPIC:-code-execution.retry.v1}" "$execution_max" sandbox sandbox "$sandbox_group"
    row "${KAFKA_EVENTS_TOPIC:-code-execution.events.v1}" "$execution_max" sandbox - -
    row "${KAFKA_DEADLETTER_TOPIC:-code-execution.deadletter.v1}" "$execution_max" sandbox - -
    row "${KAFKA_CONTENT_TOPIC:-content.content-item-events}" "$legacy_max" content - -
    row "${KAFKA_CATALOG_ACTIVITY_TOPIC:-catalog.activity.v1}" "$legacy_max" entities feed "$feed_group"
    row "${KAFKA_TASK_ACTIVITY_TOPIC:-task.activity.v1}" "$legacy_max" tasks feed "$feed_group"
    row learning.outcome.v1 "$metadata_max" tasks recommendations recommendations-learning-evidence-v1
    row learning.goal.v1 "$metadata_max" recommendations - -
    row "$material_topic" "$metadata_max" content feed "$feed_group"
    row "${KAFKA_CONTENT_LEARNING_TOPIC:-content.learning.v1}" "$metadata_max" content recommendations recommendations-learning-evidence-v1
    row "${KAFKA_CONTENT_CONTRIBUTION_TOPIC:-content.contribution.v1}" "$metadata_max" content - -
    row user.deleted.v1 "$metadata_max" - - -
    if [ "$v2" = true ]; then
        row code-execution.requests.v2 "$execution_max" tasks-v2 sandbox-v2 sandbox-code-execution-v2
        row code-execution.results.v2 "$execution_max" sandbox-v2 tasks-v2 tasks-code-results-v2
        row code-execution.retry.v2 "$execution_max" sandbox-v2 sandbox-v2 sandbox-code-execution-v2
        row code-execution.events.v2 "$execution_max" sandbox-v2 - -
        row code-execution.deadletter.v2 "$execution_max" sandbox-v2 - -
    fi
}

rows=$(catalog)
expected=14
[ "$v2" = false ] || expected=19
printf '%s\n' "$rows" | awk -v expected="$expected" '
    NF != 5 { bad = 1 }
    {
        # Dots and underscores collide in Kafka metric names.
        key = $1
        gsub(/[.]/, "_", key)
        if (topics[key]++) bad = 1
        if ($5 != "-" && readers[$5] && readers[$5] != $4) bad = 1
        readers[$5] = $4
        if ($1 ~ /^code-execution\.(requests|results|retry|events|deadletter)\.v2$/ &&
            $3 != "tasks-v2" && $3 != "sandbox-v2") bad = 1
        if ($5 ~ /^(tasks-code-results|sandbox-code-execution)-v2$/ &&
            $4 != "tasks-v2" && $4 != "sandbox-v2") bad = 1
    }
    END { exit (bad || NR != expected) }
' || die 'invalid or conflicting topic/group name'

kafka() {
    command=$1
    shift
    if [ "$mode" = secure ]; then
        "/opt/kafka/bin/$command.sh" --bootstrap-server "$bootstrap" --command-config "$admin_config" "$@"
    else
        "/opt/kafka/bin/$command.sh" --bootstrap-server "$bootstrap" "$@"
    fi
}

if [ "$mode" = plan ]; then
    printf 'partitions=%s replicas=%s retention.ms=%s\n' "$partitions" "$replicas" "$retention"
    printf 'topic max.message.bytes writer reader group\n%s\n' "$rows"
    exit 0
fi

if [ "$mode" = secure ]; then
    kafka kafka-acls --list >/dev/null || die 'broker authorizer is required'
fi

existing=$(kafka kafka-topics --list)
while IFS=' ' read -r topic limit writer reader group; do
    if printf '%s\n' "$existing" | grep -Fxq -- "$topic"; then
        continue
    fi
    kafka kafka-topics --create --if-not-exists --topic "$topic" \
        --partitions "$partitions" --replication-factor "$replicas" \
        --config "retention.ms=$retention" --config "max.message.bytes=$limit" --config cleanup.policy=delete
done <<EOF
$rows
EOF

[ "$mode" = secure ] || exit 0

# Do not change existing topic settings during a rollout.
description=$(kafka kafka-topics --describe)
printf '%s\n' "$description" | awk -v p="$partitions" -v r="$replicas" -v ttl="$retention" -v rows="$rows" '
    BEGIN {
        split(rows, lines, "\n")
        for (i in lines) {
            split(lines[i], fields, " ")
            limits[fields[1]] = fields[2]
        }
    }
    $1 == "Topic:" && $2 in limits {
        for (i = 3; i < NF; i++) {
            if ($i == "PartitionCount:" && $(i+1) == p) partitions[$2] = 1
            if ($i == "ReplicationFactor:" && $(i+1) == r) replicas[$2] = 1
            if ($i == "Configs:") {
                n = split($(i+1), configs, ",")
                for (j = 1; j <= n; j++) {
                    if (configs[j] == "retention.ms=" ttl) retention[$2] = 1
                    if (configs[j] == "max.message.bytes=" limits[$2]) size[$2] = 1
                    if (configs[j] == "cleanup.policy=delete" && (j == n || configs[j+1] ~ /=/)) cleanup[$2] = 1
                }
            }
        }
    }
    END {
        for (name in limits)
            if (!partitions[name] || !replicas[name] || !retention[name] || !size[name] || !cleanup[name]) {
                print "Kafka topics: configuration drift on " name "; existing topic unchanged" > "/dev/stderr"
                bad = 1
            }
        exit bad
    }
'

acl() {
    operation=$1
    principal=$2
    shift 2
    kafka kafka-acls --add --allow-principal "User:$principal" --operation "$operation" \
        --resource-pattern-type literal "$@" >/dev/null
}

# Combine grants that have the same principal and operation.
grants() {
    printf '%s\n' "$rows" | awk -v material_topic="$material_topic" '
        $3 != "-" { print "Write", $3, "--topic", $1 }
        $4 != "-" { print "Read", $4, "--topic", $1 }
        $5 != "-" { print "Read", $4, "--group", $5 }
        $1 == material_topic { print "Read", "recommendations", "--topic", $1 }
    ' | sort -u | awk '
        { key = $1 " " $2; resources[key] = resources[key] " " $3 " " $4 }
        END { for (key in resources) print key resources[key] }
    '
}

while IFS= read -r grant; do
    # Names were validated above and contain no shell metacharacters or whitespace.
    set -f
    set -- $grant
    set +f
    acl "$@"
done <<EOF
$(grants)
EOF
