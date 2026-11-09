#!/usr/bin/env sh
set -eu

# Собирает docker-compose образы партиями по PARALLEL (3-й аргумент), последовательно
# между партиями. Защита от Docker OOM: полный параллелизм всех Go-build (16 образов)
# не помещается в маленькую RAM (см. Makefile BUILD_PARALLEL). У `docker compose build`
# в Compose v5 нет флага --parallel, поэтому группируем имена сервисов вручную.
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"
SERVICES="${2:-}"
PARALLEL="${3:-2}"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

COMPOSE="docker compose --env-file $ENV_FILE -f $ROOT/docker-compose.yml"

i=0
group=""
for svc in $SERVICES; do
  i=$((i + 1))
  group="$group $svc"
  if [ "$i" -ge "$PARALLEL" ]; then
    # shellcheck disable=SC2086 # group намеренно разбивается по словам
    $COMPOSE build $group
    i=0
    group=""
  fi
done

# Остаток партии
if [ -n "$group" ]; then
  # shellcheck disable=SC2086
  $COMPOSE build $group
fi
