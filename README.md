# Infra

Локальное окружение Overmindv запускает `users`, `entities`, `tasks-it`, `task-hunter`, `api-gateway`, `frontend`, Kafka и отдельный PostgreSQL для каждого сервиса-владельца данных.

## Быстрый запуск

Нужен только запущенный Docker Desktop. Из каталога `infra` выполните:

```bash
make up
```

Команда сама:

- создаст `.env`, если его ещё нет;
- сгенерирует пароли PostgreSQL, JWT secret и внутренние service tokens;
- соберёт образы;
- запустит PostgreSQL, применит миграции и поднимет Kafka в KRaft-режиме;
- создаст Kafka topic'и запросов на выполнение кода и результатов;
- поднимет backend, gateway и frontend;
- дождётся healthchecks;
- покажет адреса и локальный пароль администратора.

Никакие API-ключи для обычной разработки не нужны. Сгенерированные значения отмечены комментарием в `.env.example`, сохраняются только в локальном `.env` и не попадают в git.

После запуска:

- frontend — `http://localhost:3000`;
- GraphQL — `http://localhost:8081/graphql`;
- Kafka с хост-машины — `localhost:29092`;
- admin email — `admin@overmindv.local`;
- сгенерированный admin password — в выводе `make up` или `make credentials`.

Если стандартный порт занят, измените только нужное значение в `.env`: `FRONTEND_PORT`, `API_GATEWAY_PORT`, `KAFKA_PORT`, `USERS_POSTGRES_PORT`, `ENTITIES_POSTGRES_PORT` или `TASKS_IT_POSTGRES_PORT`.

## Основные команды

```bash
make up           # собрать и запустить всё
make status       # показать состояние контейнеров
make logs         # показать общие логи
make kafka-check  # проверить topic'и и полный producer/consumer flow
make credentials  # показать локальный URL и admin credentials
make down         # остановить и сохранить базы
make clean        # остановить и удалить локальные базы/volumes
make integration  # проверить полный GraphQL-сценарий
```

`tasks-it` и `task-hunter` доступны только во внутренней Docker-сети. Frontend обращается только к `api-gateway`.

## Kafka для выполнения кода

Используется официальный закреплённый образ `apache/kafka:4.3.1` в KRaft-режиме, поэтому ZooKeeper не нужен. Topic'и создаются автоматически и идемпотентно после готовности брокера:

- `code-execution.requests.v1` — `tasks-it` публикует запросы, `sandbox` читает их;
- `code-execution.results.v1` — `sandbox` публикует результаты, `tasks-it` читает их.

Оба topic'а имеют по три partition, replication factor `1` для одноброкерного окружения и retention семь суток. Автоматическое создание произвольных topic'ов отключено, чтобы опечатка в имени не создавала новый канал данных.

Bootstrap servers:

- из контейнеров Docker Compose — `kafka:9092`;
- с хост-машины — `localhost:29092` или порт из `KAFKA_PORT`;
- внутри namespace Kubernetes — `kafka:9092`.

Если команда `sandbox` запускает свой контейнер отдельным Compose-проектом, его нужно подключить к существующей external network `overmindv_default` и использовать `kafka:9092`. Публикация host-порта для такого подключения не нужна.

Для будущей интеграции используйте UUID решения как Kafka message key: так запрос и повторные события одного решения сохраняют порядок в одной partition. Рекомендуемые consumer groups: `sandbox-code-execution-v1` для запросов и `tasks-it-code-results-v1` для результатов. Имена topic'ов и параметры хранения задаются через `KAFKA_REQUESTS_TOPIC`, `KAFKA_RESULTS_TOPIC`, `KAFKA_TOPIC_PARTITIONS` и `KAFKA_RETENTION_MS`.

Локальные listeners используют `PLAINTEXT` и предназначены только для разработки. В Kubernetes Kafka не публикуется наружу, а NetworkPolicy разрешает входящие соединения только от pod'ов `tasks-it`, `sandbox`, самой Kafka и Job создания topic'ов. Сам сервис `sandbox` в этом репозитории не разворачивается и не изменяется.

## Опциональный Telegram

Telegram-сбор в `task-hunter` по умолчанию выключен. Это позволяет запускать весь основной стек без внешнего аккаунта и MTProto session. API управления очередью и сбор задач по публичным website URL при этом работают.

Для текущей разработки рекомендуется оставить `TASK_HUNTER_TELEGRAM_ENABLED=false` и собирать задачи с сайтов. Bot API не даёт читать произвольную историю чужих публичных каналов: бот получает новые `channel_post` только в каналах, куда его добавили. Поэтому BotFather token не решает задачу сбора существующих постов; отдельный режим пересылки сообщений боту можно добавить позже вместе с фильтром «пост является задачей».

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

Для website-сбора внешние ключи не требуются. LeetCode и CodeRun читаются напрямую, а публичный Reader fallback Codeforces уже задан в `TASK_HUNTER_CODEFORCES_READER_URL`; при необходимости его можно заменить self-hosted совместимым endpoint.

## Миграции и данные

Каждый сервис использует собственную PostgreSQL. Перед запуском приложения отдельный одноразовый контейнер выполняет `goose up`; вручную накатывать миграции не требуется.

`make down` сохраняет данные. Только `make clean` удаляет PostgreSQL volumes и создаёт чистое окружение при следующем запуске.

## Проверка

```bash
make lint
make kafka-check
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

Секреты создаются через `kubectl` и не хранятся в манифестах. Kafka разворачивается как однорепликовый StatefulSet с PVC `5Gi`; Job `kafka-topics` ждёт готовности брокера и создаёт оба topic'а. Kafka, `tasks-it` и `task-hunter` не публикуются через Ingress.
