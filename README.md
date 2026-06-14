# ratchet

`ratchet` хранит baseline инфраструктуры Overmindv.

## Цели текущего этапа

- локально поднять первый вертикальный срез;
- иметь единый layout для `development`, `staging`, `production`;
- не перегружать инфраструктуру до появления реальных доменных зависимостей.

## Что есть сейчас

- `docker-compose/local.yaml` — локальный запуск `arcee`, `laserbeak`, `soundwave`;
- `k8s/base` — общая Kubernetes-база;
- `k8s/dev`, `k8s/staging`, `k8s/production` — overlays окружений;
- `scripts/render.sh` — быстрый рендер overlay через kustomize;
- `.github/workflows/ci.yml` — validation pipeline для compose и overlays;
- `.github/workflows/deploy.yml` — manual GitHub deploy pipeline для `development`, `staging`, `production`.

## Env model

- `local` — разработка на машине через compose или прямые запуски;
- `development` — интеграционная среда команды;
- `staging` — предрелизная среда;
- `production` — боевая среда.

## Быстрый старт

```bash
docker compose -f docker-compose/local.yaml up --build
```

## GitHub deploy flow

CD-часть сейчас живёт здесь, а не в сервисных репозиториях:

- workflow `Ratchet Deploy` запускается вручную;
- kubeconfig берётся из GitHub Secrets;
- workflow применяет нужный overlay через `kubectl apply -k`.
