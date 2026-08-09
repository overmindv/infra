# Infra

Репозиторий содержит локальный Docker Compose, Kubernetes-манифесты, сборку образов и интеграционную проверку Overmindv.

## Состав окружения

Основной стек запускает:

- `users` — пользователи, роли и JWT;
- `entities` — университеты, программы, курсы и темы;
- `tasks-it` — версионированные IT-тесты и история решений;
- `api-gateway` — внешний GraphQL API;
- `frontend` — web-интерфейс;
- отдельный PostgreSQL для каждого сервиса-владельца данных.

`content` и `media` пока оставлены только в опциональном Compose-профиле `future`. Новые артефакты используют актуальные названия сервисов. Legacy-названия переменных `ARCEE_*` и `IRONHIDE_*` встречаются только в runtime-контракте существующего `api-gateway`; инфраструктурные ресурсы и сетевые имена уже новые.

## Локальный запуск

```bash
cp .env.example .env
make up
```

После запуска доступны:

- frontend — `http://localhost:3000`;
- GraphQL API — `http://localhost:8081/graphql`;
- health api-gateway — `http://localhost:8081/health`;
- PostgreSQL `users` — `localhost:5432`;
- PostgreSQL `entities` — `localhost:5433`;
- PostgreSQL `tasks-it` — `localhost:5434`.

`tasks-it` не публикует HTTP-порт на хост и доступен только по имени `http://tasks-it:8080` во внутренней сети Compose. Пользовательские запросы идут через `api-gateway`.

Для каждого backend-сервиса Compose сначала ждёт PostgreSQL, затем выполняет `goose up` отдельным migration-контейнером. Приложение стартует только после успешного завершения миграций.

## Проверка и логи

```bash
make lint
make test
make integration
make logs
```

`make integration` проверяет полный путь через GraphQL: регистрацию и роли, создание каталога, создание и публикацию IT-теста, скрытие правильного ответа, решение теста и историю решений.

Остановить окружение и удалить локальные volumes:

```bash
make down
```

## Kubernetes

Манифесты создают три независимых PostgreSQL, init-контейнеры миграций, внутренний Service `tasks-it:8080`, probes и ресурсы для всех workloads. `tasks-it` намеренно не добавлен в Ingress.

Перед локальным развёртыванием задайте секреты:

```bash
export USERS_POSTGRES_PASSWORD='change-me'
export ENTITIES_POSTGRES_PASSWORD='change-me'
export TASKS_IT_POSTGRES_PASSWORD='change-me'
export JWT_SECRET='change-me-long-random-value'
export BOOTSTRAP_SUPERUSER_EMAIL='admin@overmindv.local'
export BOOTSTRAP_SUPERUSER_PASSWORD='change-me'
make deploy-k8s
```

Скрипт создаёт Kubernetes Secret через `kubectl`; значения секретов не хранятся в манифестах. Файл `k8s/secret.yaml` служит только примером и не входит в `kustomization.yaml`.

Проверить итоговые ресурсы без применения:

```bash
make render-k8s
```

Для Ingress добавьте `overmindv.local` в локальный DNS или `/etc/hosts`. Внешние маршруты ведут только к `frontend` и `api-gateway`.

## Образы

```bash
make build
IMAGE_REGISTRY=registry.example.com/overmindv IMAGE_TAG=latest make push
```

Собираются образы `users`, `entities`, `tasks-it`, `api-gateway` и `frontend`.
