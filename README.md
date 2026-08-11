# Infra

Локальное окружение Overmindv запускает `users`, `entities`, `tasks-it`, `task-hunter`, `api-gateway`, `frontend` и отдельный PostgreSQL для каждого сервиса-владельца данных.

## Быстрый запуск

Нужен только запущенный Docker Desktop. Из каталога `infra` выполните:

```bash
make up
```

Команда сама:

- создаст `.env`, если его ещё нет;
- сгенерирует пароли PostgreSQL, JWT secret и внутренние service tokens;
- соберёт образы;
- запустит PostgreSQL и применит миграции;
- поднимет backend, gateway и frontend;
- дождётся healthchecks;
- покажет адреса и локальный пароль администратора.

Никакие API-ключи для обычной разработки не нужны. Сгенерированные значения отмечены комментарием в `.env.example`, сохраняются только в локальном `.env` и не попадают в git.

После запуска:

- frontend — `http://localhost:3000`;
- GraphQL — `http://localhost:8081/graphql`;
- admin email — `admin@overmindv.local`;
- сгенерированный admin password — в выводе `make up` или `make credentials`.

Если стандартный порт занят, измените только нужное значение в `.env`: `FRONTEND_PORT`, `API_GATEWAY_PORT`, `USERS_POSTGRES_PORT`, `ENTITIES_POSTGRES_PORT` или `TASKS_IT_POSTGRES_PORT`.

## Основные команды

```bash
make up           # собрать и запустить всё
make status       # показать состояние контейнеров
make logs         # показать общие логи
make credentials  # показать локальный URL и admin credentials
make down         # остановить и сохранить базы
make clean        # остановить и удалить локальные базы/volumes
make integration  # проверить полный GraphQL-сценарий
```

`tasks-it` и `task-hunter` доступны только во внутренней Docker-сети. Frontend обращается только к `api-gateway`.

## Опциональный Telegram

Telegram-сбор в `task-hunter` по умолчанию выключен. Это позволяет запускать весь основной стек без внешнего аккаунта и MTProto session. API управления очередью и сбор задач по публичным website URL при этом работают.

Если нужен сбор из Telegram:

1. Откройте [my.telegram.org](https://my.telegram.org), войдите по номеру телефона и выберите **API development tools**.
2. Создайте приложение. Telegram покажет `api_id` и `api_hash` — это не BotFather token и не токен Telegram-бота.
3. В локальном `.env` заполните:

```dotenv
TASK_HUNTER_TELEGRAM_API_ID=123456
TASK_HUNTER_TELEGRAM_API_HASH=ваш_api_hash
```

4. Создайте закрытую MTProto session:

```bash
TELEGRAM_PHONE=+79990000000 make telegram-login
```

Утилита попросит одноразовый код из Telegram. Если на аккаунте включён 2FA-пароль:

```bash
TELEGRAM_PHONE=+79990000000 TELEGRAM_2FA_PASSWORD='ваш пароль' make telegram-login
```

5. После успешной авторизации установите в `.env`:

```dotenv
TASK_HUNTER_TELEGRAM_ENABLED=true
```

6. Выполните `make up` ещё раз.

Session хранится в `infra/.local/task-hunter/telegram.session`, имеет локальные права `0600` и исключена из git. Не отправляйте этот файл другим разработчикам: он эквивалентен активной авторизации Telegram.

Других внешних ключей, BotFather token или Telegram bot API для текущего стека не требуется.

## Миграции и данные

Каждый сервис использует собственную PostgreSQL. Перед запуском приложения отдельный одноразовый контейнер выполняет `goose up`; вручную накатывать миграции не требуется.

`make down` сохраняет данные. Только `make clean` удаляет PostgreSQL volumes и создаёт чистое окружение при следующем запуске.

## Проверка

```bash
make lint
make test
make integration
```

Интеграционный сценарий проверяет регистрацию, роли, каталог, создание и публикацию IT-теста, решение и историю пользователя.

## Kubernetes

Kubernetes-развёртывание остаётся явной production-операцией и требует секреты окружения, включая Telegram credentials:

```bash
export USERS_POSTGRES_PASSWORD='...'
export ENTITIES_POSTGRES_PASSWORD='...'
export TASKS_IT_POSTGRES_PASSWORD='...'
export TASK_HUNTER_POSTGRES_PASSWORD='...'
export TASK_HUNTER_INGEST_TOKEN='...'
export TASK_HUNTER_GATEWAY_TOKEN='...'
export TASK_HUNTER_TELEGRAM_API_ID='...'
export TASK_HUNTER_TELEGRAM_API_HASH='...'
export JWT_SECRET='...'
export BOOTSTRAP_SUPERUSER_EMAIL='admin@example.com'
export BOOTSTRAP_SUPERUSER_PASSWORD='...'
make deploy-k8s
```

Секреты создаются через `kubectl` и не хранятся в манифестах. `tasks-it` и `task-hunter` не публикуются через Ingress.
