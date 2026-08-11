#!/usr/bin/env sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
ENV_FILE="${1:-$ROOT/.env}"

case "$ENV_FILE" in
  /*) ;;
  *) ENV_FILE="$ROOT/$ENV_FILE" ;;
esac

# env_value возвращает значение или указанный fallback.
env_value() {
  value="$(awk -v key="$1" 'index($0, key "=") == 1 { print substr($0, length(key) + 2); exit }' "$ENV_FILE")"
  if [ -n "$value" ]; then
    printf '%s' "$value"
  else
    printf '%s' "$2"
  fi
}

frontend_port="$(env_value FRONTEND_PORT 3000)"
gateway_port="$(env_value API_GATEWAY_PORT 8081)"
admin_email="$(env_value BOOTSTRAP_SUPERUSER_EMAIL admin@overmindv.local)"
admin_password="$(env_value BOOTSTRAP_SUPERUSER_PASSWORD '')"
telegram_enabled="$(env_value TASK_HUNTER_TELEGRAM_ENABLED false)"

echo
echo "Overmindv готов:"
echo "  Frontend:    http://localhost:$frontend_port"
echo "  GraphQL:     http://localhost:$gateway_port/graphql"
echo "  Admin email: $admin_email"
echo "  Admin pass:  $admin_password"
echo "  Telegram:    $telegram_enabled"
echo
echo "Повторно показать эти данные: make credentials"
