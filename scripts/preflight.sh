#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

# env_value читает одну настройку без загрузки env как shell-кода.
env_value() {
  awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE"
}

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker не найден. Установите Docker Desktop: https://docs.docker.com/desktop/" >&2
  exit 1
fi

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 недоступен. Обновите Docker Desktop." >&2
  exit 1
fi

# check_docker_daemon ограничивает ожидание неотвечающего Docker Desktop.
check_docker_daemon() {
  docker info >/dev/null 2>&1 &
  docker_pid=$!
  attempts=0

  while kill -0 "$docker_pid" 2>/dev/null; do
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 10 ]; then
      kill "$docker_pid" 2>/dev/null || true
      wait "$docker_pid" 2>/dev/null || true
      echo "Docker Desktop не отвечает. Запустите его и повторите make up." >&2
      exit 1
    fi
    sleep 1
  done

  if ! wait "$docker_pid"; then
    echo "Docker daemon не запущен. Откройте Docker Desktop и повторите make up." >&2
    exit 1
  fi
}

check_docker_daemon

if [ "$(env_value TASK_HUNTER_TELEGRAM_ENABLED)" = "true" ]; then
  if [ -z "$(env_value TASK_HUNTER_TELEGRAM_API_ID)" ] || [ -z "$(env_value TASK_HUNTER_TELEGRAM_API_HASH)" ]; then
    echo "Telegram включён, но API ID/hash не заполнены в $ENV_FILE." >&2
    echo "Получите их на https://my.telegram.org/apps, затем выполните make telegram-login." >&2
    exit 1
  fi
  if [ ! -s "$ROOT/.local/task-hunter/telegram.session" ]; then
    echo "Telegram включён, но session не создана." >&2
    echo "Выполните: TELEGRAM_PHONE=+79990000000 make telegram-login" >&2
    exit 1
  fi
fi
