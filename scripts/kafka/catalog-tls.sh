#!/bin/sh
set -eu
export LC_ALL=C

password() {
    case "$1" in
        ''|*[!a-fA-F0-9]*) return 1 ;;
    esac
    [ "${#1}" -eq 64 ]
}

bad_password() {
    printf '%s\n' 'Catalog Kafka requires separate 64-character hex passwords' >&2
    exit 1
}

plain='org.apache.kafka.common.security.plain.PlainLoginModule required'
admin=${OVM_KAFKA_CATALOG_ADMIN_PASSWORD:-}
password "$admin" || bad_password

case "${1:-}" in
    broker)
        broker=${OVM_KAFKA_CATALOG_BROKER_PASSWORD:-}
        entities=${OVM_KAFKA_CATALOG_ENTITIES_PASSWORD:-}
        password "$broker" && password "$entities" &&
            [ "$broker" != "$admin" ] && [ "$broker" != "$entities" ] &&
            [ "$admin" != "$entities" ] || bad_password
        export KAFKA_LISTENER_NAME_CONTROLLER_PLAIN_SASL_JAAS_CONFIG="$plain username=\"broker\" password=\"$broker\" user_broker=\"$broker\";"
        export KAFKA_LISTENER_NAME_REPLICA_PLAIN_SASL_JAAS_CONFIG="$KAFKA_LISTENER_NAME_CONTROLLER_PLAIN_SASL_JAAS_CONFIG"
        users="user_catalog_admin=\"$admin\" user_entities=\"$entities\""
        if [ "${OVM_KAFKA_FEED_PASSWORD+x}" ]; then
            feed=$OVM_KAFKA_FEED_PASSWORD
            password "$feed" && [ "$feed" != "$broker" ] && [ "$feed" != "$admin" ] &&
                [ "$feed" != "$entities" ] || bad_password
            users="$users user_feed=\"$feed\""
        fi
        export KAFKA_LISTENER_NAME_SECURE_PLAIN_SASL_JAAS_CONFIG="$plain $users;"
        unset OVM_KAFKA_CATALOG_BROKER_PASSWORD OVM_KAFKA_CATALOG_ADMIN_PASSWORD OVM_KAFKA_CATALOG_ENTITIES_PASSWORD
        unset OVM_KAFKA_FEED_PASSWORD
        exec /__cacert_entrypoint.sh /etc/kafka/docker/run
        ;;
    provision)
        umask 077
        client=$(mktemp)
        trap 'rm -f -- "$client"' EXIT
        trap 'exit 1' HUP INT TERM
        {
            printf '%s\n' 'security.protocol=SASL_SSL' 'sasl.mechanism=PLAIN' \
                'ssl.truststore.type=PEM' 'ssl.truststore.location=/run/kafka/ca.pem'
            printf 'sasl.jaas.config=%s username="catalog_admin" password="%s";\n' "$plain" "$admin"
        } > "$client"
        case "${OVM_KAFKA_FEED_TLS:-false}" in
            true) mode=catalog-rollout-feed ;;
            rollback) mode=catalog-rollback-feed ;;
            false) mode=catalog-rollout ;;
            *) printf '%s\n' 'Invalid Kafka Feed TLS mode' >&2; exit 1 ;;
        esac
        sh /opt/overmind/kafka/topics.sh "$mode" kafka:9094 "$client"
        ;;
    *) exit 2 ;;
esac
