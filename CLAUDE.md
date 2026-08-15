# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Что это за репозиторий

`infra` — оркестрационный хаб платформы Overmindv (IT-тесты, задачи, CodeRun/LeetCode). Здесь **нет кода самих сервисов**: инфраструктурный репозиторий определяет локальный dev-стек (Docker Compose), Kubernetes-развёртывание, CI, общие скрипты и интеграционные тесты, а сервисы собираются из **соседних git-репозиториев** (`../users`, `../entities`, `../tasks`, `../task-hunter`, `../api-gateway`, `../frontend`).

Правка логики сервиса — это правка соседнего репозитория, а не этого. В `infra` вы меняете то, *как всё собирается и разворачивается*.

## Сервисы и поток данных

- `users` — Go, GraphQL, JWT auth, бутстрап суперпользователя.
- `entities` — Go, каталог сущностей.
- `tasks` — Go, IT-тесты, публикует запросы на выполнение кода в Kafka.
- `task-hunter` (env‑префикс `PARSER_*`) — Go, сбор задач с сайтов/Telegram, отдаёт их в `tasks`.
- `api-gateway` (оборачивает все бэкенды, единственная публичная точка): frontend → gateway → сервисы.
- `frontend` — npm/Vite.

Поток в UI: `frontend` (`:3000`) обращается только к `api-gateway` (`:8081/graphql`), gateway валидирует JWT и проксирует в `users`/`entities`/`tasks`/`task-hunter`. `tasks` и `task-hunter` доступны **только** во внутренней Docker-сети.

### Kafka (выполнение кода)

- Topic `code-execution.requests.v1` — `tasks` публикует запросы, их читает `sandbox`.
- Topic `code-execution.results.v1` — `sandbox` публикует результаты, их читает `tasks`.
- Kafka в KRaft-режиме (без ZooKeeper), 3 partition, retention 7 суток, `KAFKA_AUTO_CREATE_TOPICS_ENABLE=false`.
- Bootstrap servers: из контейнеров Compose и в namespace k8s — `kafka:9092`; с хоста — `localhost:29092`.
- **`sandbox` в этом репозитории не разворачивается и не меняется.**
- Message key — UUID решения (порядок в partition). Consumer groups: `sandbox-code-execution-v1` (запросы), `tasks-code-results-v1` (результаты).

## Локальная разработка

Нужен только Docker Desktop. Всё из корня `infra`:

```bash
make up              # создать .env, собрать образы, поднять весь стек, применить миграции, дождаться healthchecks
make status          # состояние контейнеров
make logs            # логи всех сервисов
make request-logs    # логи обработчиков запросов (gateway/entities/tasks/task-hunter)
make credentials     # URL и локальный admin
make kafka-check     # проверить topics + producer/consumer flow
make integration     # сквозной GraphQL-сценарий на поднятом стеке
make down            # остановить и сохранить базы
make clean           # остановить и удалить volumes (полностью чистое окружение)
```

`make up` генерирует `.env` из `.env.example` (`scripts/prepare-env.sh`), заполняя `__GENERATE__`-маркеры случайными секретами. **Секреты живут только в `.env`, который в git не попадает** (включён в `.gitignore`). `users`-сервис бутстрапит `admin@overmindv.local`.

### Миграции

У каждого сервиса свой PostgreSQL. Миграции — `goose up` — выполняет одноразовый контейнер `*-migrate`, запускающийся до приложения (`depends_on: service_completed_successfully`). Вручную накатывать миграции не требуется.

## Тесты и линт

```bash
make lint                          # валидация compose + kubectl kustomize + sh -n скриптов
make test                          # прогоняет тесты ВСЕХ сервисов (делегирует в соседние репозитории) + typecheck frontend
make integration                   # сквозной GraphQL-сценарий (требует поднятый stack)
```

`make test` делегирует: `make -C ../users test`, `go test ./...` в `../entities` и `../task-hunter`, `make -C ../tasks test`, `make -C ../api-gateway test`, `npm run typecheck` и `npm run test:ci` в `../frontend`. Тест отдельного сервиса запускается из его собственного репозитория.

## Kubernetes

Production-развёртывание — явная операция через `kubectl`, не через Flagger/Helm. Манифесты — kustomize в `k8s/` (`kubectl kustomize k8s`).

- Секреты (Postgres-пароли, `JWT_SECRET`, `TASK_HUNTER_*`, Telegram) **не лежат в манифестах** — создаются вручную как secret `overmindv-secrets` (см. `make deploy-k8s` / `scripts/deploy-k8s.sh`).
- Kafka — однорепликовый StatefulSet с PVC `5Gi`; Job `kafka-topics` создаёт topics идемпотентно.
- Kafka, `tasks`, `task-hunter` наружу (через Ingress) не публикуются; NetworkPolicy пускает в Kafka только pod'ы `tasks`, `sandbox`, самой Kafka и Job.
- Кластер: `kind` (кластер `overmindv`) или `minikube`; образы подгружаются автоматически.
- CI: `ci.yml` (lint + kustomize validation на PR/push); `deploy.yml` — **устарел**.

## Telegram (опционально)

Сбор из Telegram в `task-hunter` выключен по умолчанию (`TASK_HUNTER_TELEGRAM_ENABLED=false`) — основной стек работает без внешнего аккаунта. Чтобы включить: заполнить `TASK_HUNTER_TELEGRAM_API_ID`/`API_HASH` (это не BotFather token), создать MTProto session через `make telegram-login` (интерактивный одноразовый код), session хранится в `.local/task-hunter/telegram.session`. Website-сбор работает без ключей; публичный Reader fallback Codeforces задаётся через `TASK_HUNTER_CODEFORCES_READER_URL`.
