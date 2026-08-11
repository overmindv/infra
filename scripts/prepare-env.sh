#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"
TEMPLATE="$ROOT/.env.example"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

if [ ! -f "$ENV_FILE" ]; then
  cp "$TEMPLATE" "$ENV_FILE"
  echo "Создан $ENV_FILE"
fi

# random_secret создаёт локальный секрет без внешних сервисов.
random_secret() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32

    return
  fi

  od -An -N32 -tx1 /dev/urandom | tr -d ' \n'
}

# env_value читает значение ключа без выполнения содержимого env-файла.
env_value() {
  awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE"
}

# replace_value атомарно заменяет или добавляет одну переменную.
replace_value() {
  key="$1"
  value="$2"
  temporary="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    index($0, key "=") == 1 {
      print key "=" value
      found = 1
      next
    }
    { print }
    END {
      if (!found) {
        print key "=" value
      }
    }
  ' "$ENV_FILE" >"$temporary"
  mv "$temporary" "$ENV_FILE"
}

# ensure_secret заменяет только пустые значения и маркер генерации.
ensure_secret() {
  key="$1"
  value="$(env_value "$key")"
  case "$value" in
    ""|__GENERATE__)
      replace_value "$key" "$(random_secret)"
      GENERATED=1
      ;;
  esac
}

GENERATED=0
for key in \
  USERS_POSTGRES_PASSWORD \
  ENTITIES_POSTGRES_PASSWORD \
  TASKS_IT_POSTGRES_PASSWORD \
  TASK_HUNTER_POSTGRES_PASSWORD \
  TASK_HUNTER_INGEST_TOKEN \
  TASK_HUNTER_GATEWAY_TOKEN \
  JWT_SECRET \
  BOOTSTRAP_SUPERUSER_PASSWORD; do
  ensure_secret "$key"
done

if [ -z "$(env_value TASK_HUNTER_TELEGRAM_ENABLED)" ]; then
  replace_value TASK_HUNTER_TELEGRAM_ENABLED false
fi
if [ -z "$(env_value LOCAL_UID)" ] || [ "$(env_value LOCAL_UID)" = "__LOCAL_UID__" ]; then
  replace_value LOCAL_UID "$(id -u)"
fi
if [ -z "$(env_value LOCAL_GID)" ] || [ "$(env_value LOCAL_GID)" = "__LOCAL_GID__" ]; then
  replace_value LOCAL_GID "$(id -g)"
fi

mkdir -p "$ROOT/.local/task-hunter"
chmod 700 "$ROOT/.local/task-hunter"

chmod 600 "$ENV_FILE"

if [ "$GENERATED" -eq 1 ]; then
  echo "Локальные пароли и service tokens сгенерированы автоматически."
fi
