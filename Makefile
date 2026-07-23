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
	$(COMPOSE) logs -f arcee ironhide laserbeak

# Логи пользовательских запросов Laserbeak и Ironhide без healthcheck-шумa
request-logs:
	mkdir -p logs/laserbeak logs/ironhide
	touch logs/laserbeak/requests.log logs/ironhide/requests.log
	tail -f logs/laserbeak/requests.log logs/ironhide/requests.log

# Быстрая проверка инфраструктурных файлов
lint:
	docker compose --env-file .env.example -f docker-compose.yml config >/dev/null
	sh -n scripts/build-images.sh scripts/deploy-k8s.sh scripts/push-images.sh tests/integration.sh

# Запуск тестов
test: lint
	$(MAKE) -C ../arcee test
	cd ../ironhide && go test ./...
	$(MAKE) -C ../laserbeak test
	npm --prefix ../soundwave run typecheck
	npm --prefix ../soundwave run test:ci

# Запуск интеграционных тестов
integration:
	./tests/integration.sh

# Build/load images and apply Kubernetes manifests
deploy-k8s:
	./scripts/deploy-k8s.sh

# Render the Kubernetes resources
render-k8s:
	kubectl kustomize k8s
