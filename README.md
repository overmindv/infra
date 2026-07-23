# Ratchet

Ratchet - инфраструктурный репозиторий Overmindv. Он отвечает за локальный Docker Compose, Kubernetes-манифесты, сборку образов и базовые проверки окружения.

## Что запускает

Основной локальный стек:

- `arcee` - пользователи, вход, роли, JWT;
- `ironhide` - каталог университетов, программ, курсов и тем;
- `laserbeak` - GraphQL API Gateway для frontend;
- `soundwave` - web frontend;
- отдельные PostgreSQL инстансы для сервисов-владельцев данных.

Внешняя точка входа backend: `http://localhost:8081/graphql`. Frontend доступен на `http://localhost:3000`.

## Запуск

```bash
cp .env.example .env
make up
```

Проверка стека:

```bash
curl http://localhost:8081/health
make integration
```

Остановка с удалением локальных volume:

```bash
make down
```

## Логи запросов

Laserbeak и Ironhide пишут пользовательские HTTP-запросы и upstream-вызовы отдельно от Docker stdout:

```bash
make request-logs
```

Файлы создаются локально в `logs/laserbeak/requests.log` и `logs/ironhide/requests.log` и не хранятся в git. Для поиска ошибки берите `request_id` из ответа GraphQL или лога Laserbeak и ищите его в обоих файлах.

## Команды

```bash
make up
make down
make build
make push
make lint
make test
make integration
make render-k8s
make deploy-k8s
```

## Особенности

Bootstrap-суперпользователь создаётся Arcee при старте, если соответствующие значения заданы в `.env`. Этот пользователь нужен для назначения первых администраторов. Администраторы управляют пользователями и каталогом через Laserbeak; обычные пользователи получают read-only функционал frontend.
