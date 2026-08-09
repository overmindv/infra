COMPOSE := docker compose -f docker-compose.yml

.PHONY: up down build push logs request-logs lint test integration deploy-k8s render-k8s

# Запуск
up:
	$(COMPOSE) up --build -d

# Остановка
down:
	$(COMPOSE) down -v --remove-orphans

# Сборка
build:
	./scripts/build-images.sh

# Push previously built images to IMAGE_REGISTRY
push:
	./scripts/push-images.sh

# Логи
logs:
	$(COMPOSE) logs -f users entities tasks-it api-gateway frontend

# Логи сервисов, обрабатывающих пользовательские запросы
request-logs:
	$(COMPOSE) logs -f api-gateway entities tasks-it

# Быстрая проверка инфраструктурных файлов
lint:
	docker compose --env-file .env.example -f docker-compose.yml config >/dev/null
	sh -n scripts/build-images.sh scripts/deploy-k8s.sh scripts/push-images.sh tests/integration.sh

# Запуск тестов
test: lint
	$(MAKE) -C ../users test
	cd ../entities && go test ./...
	$(MAKE) -C ../tasks-it test
	$(MAKE) -C ../api-gateway test
	npm --prefix ../frontend run typecheck
	npm --prefix ../frontend run test:ci

# Запуск интеграционных тестов
integration:
	./tests/integration.sh

# Build/load images and apply Kubernetes manifests
deploy-k8s:
	./scripts/deploy-k8s.sh

# Render the Kubernetes resources
render-k8s:
	kubectl kustomize k8s
