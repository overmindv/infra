COMPOSE := docker compose -f docker-compose.yml

.PHONY: up down build push logs test integration deploy-k8s render-k8s

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
	$(COMPOSE) logs -f arcee laserbeak

# Запуск тестов
test: 
	docker compose --env-file .env.example -f docker-compose.yml config >/dev/null
	$(MAKE) -C ../arcee test
	$(MAKE) -C ../laserbeak test
	npm --prefix ../soundwave run typecheck
	npm --prefix ../soundwave run test:ci

# Запустк интеграционных тестов
integration:
	./tests/integration.sh

# Build/load images and apply Kubernetes manifests
deploy-k8s:
	./scripts/deploy-k8s.sh

# Render the Kubernetes resources
render-k8s:
	kubectl kustomize k8s
