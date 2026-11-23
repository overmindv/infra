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

sandbox_credentials() {
    case "${OVM_KAFKA_SANDBOX_TLS:-false}" in
        true)
            worker=$(cat /run/kafka/worker/password)
            password "$worker" && [ "$worker" != "$admin" ] || bad_password
            printf 'SCRAM-SHA-256=iterations=8192,password=%s\n' "$worker" > "$scram"
            unset worker
            # Kafka can include the credential property in its error output.
            if ! sandbox_scram --alter --add-config-file "$scram" >/dev/null 2>&1; then
                printf '%s\n' 'Failed to provision Sandbox Kafka SCRAM credential' >&2
                exit 1
            fi
            ;;
        rollback)
            description=$(sandbox_scram --describe)
            if printf '%s\n' "$description" | grep -Fq 'SCRAM-SHA-256='; then
                sandbox_scram --alter --delete-config SCRAM-SHA-256 >/dev/null
            fi
            ;;
        false) ;;
        *) printf '%s\n' 'Invalid Kafka Sandbox TLS mode' >&2; exit 1 ;;
    esac
}

sandbox_scram() {
    /opt/kafka/bin/kafka-configs.sh --bootstrap-server kafka:9094 --command-config "$client" \
        --entity-type users --entity-name sandbox "$@"
}

plain='org.apache.kafka.common.security.plain.PlainLoginModule required'
admin=${OVM_KAFKA_CATALOG_ADMIN_PASSWORD:-}
password "$admin" || bad_password

case "${1:-}" in
    broker)
        broker=${OVM_KAFKA_CATALOG_BROKER_PASSWORD:-}
        entities=${OVM_KAFKA_CATALOG_ENTITIES_PASSWORD:-}
        seen="$admin"
        check_distinct() {
            password "$1" || bad_password
            case " $seen " in *" $1 "*) bad_password ;; esac
            seen="$seen $1"
        }
        check_distinct "$broker"
        check_distinct "$entities"
        export KAFKA_LISTENER_NAME_CONTROLLER_PLAIN_SASL_JAAS_CONFIG="$plain username=\"broker\" password=\"$broker\" user_broker=\"$broker\";"
        export KAFKA_LISTENER_NAME_REPLICA_PLAIN_SASL_JAAS_CONFIG="$KAFKA_LISTENER_NAME_CONTROLLER_PLAIN_SASL_JAAS_CONFIG"
        users="user_catalog_admin=\"$admin\" user_entities=\"$entities\""
        if [ "${OVM_KAFKA_FEED_PASSWORD+x}" ]; then
            feed=$OVM_KAFKA_FEED_PASSWORD
            check_distinct "$feed"
            users="$users user_feed=\"$feed\""
        fi
        if [ "${OVM_KAFKA_CONTENT_PASSWORD+x}" ]; then
            content=$OVM_KAFKA_CONTENT_PASSWORD
            check_distinct "$content"
            users="$users user_content=\"$content\""
        fi
        export KAFKA_LISTENER_NAME_SECURE_PLAIN_SASL_JAAS_CONFIG="$plain $users;"
        unset OVM_KAFKA_CATALOG_BROKER_PASSWORD OVM_KAFKA_CATALOG_ADMIN_PASSWORD OVM_KAFKA_CATALOG_ENTITIES_PASSWORD
        unset OVM_KAFKA_FEED_PASSWORD OVM_KAFKA_CONTENT_PASSWORD
        exec /__cacert_entrypoint.sh /etc/kafka/docker/run
        ;;
    provision)
        umask 077
        work=$(mktemp -d)
        client=$work/client.properties
        scram=$work/scram.properties
        trap 'rm -f -- "$client" "$scram"; rmdir "$work"' EXIT
        trap 'exit 1' HUP INT TERM
        {
            printf '%s\n' 'security.protocol=SASL_SSL' 'sasl.mechanism=PLAIN' \
                'ssl.truststore.type=PEM' 'ssl.truststore.location=/run/kafka/ca.pem'
            printf 'sasl.jaas.config=%s username="catalog_admin" password="%s";\n' "$plain" "$admin"
        } > "$client"
        sandbox_credentials
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
