#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${INFRA_ENV_FILE:-$ROOT/.env}"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

# env_value читает настройку Kafka без выполнения содержимого env-файла.
env_value() {
  value="$(awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE")"
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$2"
  fi
}

requests_topic="$(env_value KAFKA_REQUESTS_TOPIC code-execution.requests.v1)"
results_topic="$(env_value KAFKA_RESULTS_TOPIC code-execution.results.v1)"
check_topic="infra.kafka-check.$(date +%s).$$"
check_message="kafka-check-$(date +%s)-$$"

# cleanup удаляет только временный topic текущего проверочного запуска.
cleanup() {
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --delete --if-exists --topic "$check_topic" \
    >/dev/null 2>&1 || true
}

trap cleanup EXIT HUP INT TERM

for topic in "$requests_topic" "$results_topic"; do
  docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T kafka \
    /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --describe --topic "$topic" \
    >/dev/null
done

docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T kafka \
  /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 --create --topic "$check_topic" \
  --partitions 1 --replication-factor 1 >/dev/null

printf '%s\n' "$check_message" | docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T kafka \
  /opt/kafka/bin/kafka-console-producer.sh --bootstrap-server kafka:9092 --topic "$check_topic" >/dev/null

received_message="$(docker compose --env-file "$ENV_FILE" -f "$ROOT/docker-compose.yml" exec -T kafka \
  /opt/kafka/bin/kafka-console-consumer.sh --bootstrap-server kafka:9092 --topic "$check_topic" \
  --from-beginning --max-messages 1 --timeout-ms 10000)"

if [ "$received_message" != "$check_message" ]; then
  echo "Kafka вернула неожиданное сообщение: $received_message" >&2
  exit 1
fi

echo "Kafka topics и producer/consumer flow: OK"
