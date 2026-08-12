#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

: "${TELEGRAM_PHONE:?Укажите телефон: TELEGRAM_PHONE=+79990000000 make telegram-login}"

api_id="$(awk -v key="TASK_HUNTER_TELEGRAM_API_ID" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE")"
api_hash="$(awk -v key="TASK_HUNTER_TELEGRAM_API_HASH" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE")"

if [ -z "$api_id" ] || [ -z "$api_hash" ]; then
  echo "Заполните TASK_HUNTER_TELEGRAM_API_ID и TASK_HUNTER_TELEGRAM_API_HASH в $ENV_FILE." >&2
  echo "Значения создаются на https://my.telegram.org/apps." >&2
  exit 1
fi

mkdir -p "$ROOT/.local/task-hunter"
chmod 700 "$ROOT/.local/task-hunter"

cd "$ROOT"
TELEGRAM_PHONE="$TELEGRAM_PHONE" TELEGRAM_2FA_PASSWORD="${TELEGRAM_2FA_PASSWORD:-}" \
  docker compose --env-file "$ENV_FILE" -f docker-compose.yml run --build --rm --no-deps \
  --entrypoint telegram-session task-hunter

chmod 600 "$ROOT/.local/task-hunter/telegram.session"
echo "Telegram session готова. Установите TASK_HUNTER_TELEGRAM_ENABLED=true и выполните make up."
