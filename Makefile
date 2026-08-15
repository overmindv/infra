ENV_FILE ?= .env
COMPOSE := docker compose --env-file $(ENV_FILE) -f docker-compose.yml
.DEFAULT_GOAL := help

.PHONY: help init up down clean restart status credentials telegram-login build push logs request-logs kafka-check lint test integration

# Краткая справка по локальному запуску
help:
	@echo "make up             Собрать и запустить весь локальный стек"
	@echo "make down           Остановить стек и сохранить данные"
	@echo "make clean          Остановить стек и удалить локальные базы"
	@echo "make status         Показать состояние контейнеров"
	@echo "make logs           Показать логи"
	@echo "make kafka-check    Проверить создание topic'ов и обмен сообщениями"
	@echo "make credentials    Показать URL и локального администратора"
	@echo "make telegram-login Создать опциональную Telegram session"

# Подготовка локального env с автоматически сгенерированными секретами
init:
	@./scripts/prepare-env.sh $(ENV_FILE)

# Сборка и запуск всего локального стека с ожиданием готовности
up: init
	@./scripts/preflight.sh $(ENV_FILE)
	@$(COMPOSE) up --build -d --wait --wait-timeout 300
	@./scripts/show-local-info.sh $(ENV_FILE)

# Остановка контейнеров с сохранением данных
down: init
	@$(COMPOSE) down --remove-orphans

# Полное удаление контейнеров и локальных баз данных
clean: init
	@$(COMPOSE) down -v --remove-orphans

# Перезапуск стека без удаления данных
restart: down up

# Состояние всех контейнеров
status: init
	@$(COMPOSE) ps

# Адреса и локальная учётная запись администратора
credentials: init
	@./scripts/show-local-info.sh $(ENV_FILE)

# Интерактивное создание Telegram MTProto session
telegram-login: init
	@./scripts/telegram-login.sh $(ENV_FILE)

# Сборка production-образов
build:
	@./scripts/build-images.sh

# Публикация ранее собранных образов
push:
	@./scripts/push-images.sh

# Логи основных сервисов
logs: init
	@$(COMPOSE) logs -f kafka users entities tasks task-hunter api-gateway frontend

# Логи сервисов, обрабатывающих пользовательские запросы
request-logs: init
	@$(COMPOSE) logs -f api-gateway entities tasks task-hunter

# Проверка Kafka и полного цикла producer/consumer на временном topic'е
kafka-check: init
	@INFRA_ENV_FILE=$(ENV_FILE) ./tests/kafka.sh

# Быстрая проверка инфраструктурных файлов
lint:
	@docker compose --env-file .env.example -f docker-compose.yml config >/dev/null
	@sh -n scripts/*.sh tests/integration.sh
	@sh -n tests/kafka.sh

# Запуск тестов сервисов
test: lint
	$(MAKE) -C ../users test
	cd ../entities && go test ./...
	$(MAKE) -C ../tasks test
	cd ../task-hunter && go test ./...
	$(MAKE) -C ../api-gateway test
	npm --prefix ../frontend run typecheck
	npm --prefix ../frontend run test:ci

# Запуск сквозного GraphQL-сценария на поднятом стеке
integration: init
	@INFRA_ENV_FILE=$(ENV_FILE) ./tests/integration.sh
