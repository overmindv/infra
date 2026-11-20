# Сборка релиза

Нужны Git, Python 3.12+, Docker с Buildx и доступ к registry. Скрипт не скачивает репозитории
и не меняет их рабочие папки. Запускайте его в workspace с обычными клонами Overmindv.

## Зафиксировать входы

В `sources.json` укажите полные SHA для `infra`, `api-gateway`, `content`, `entities`, `feed`,
`frontend`, `media`, `recommendations`, `sandbox`, `task-hunter`, `tasks`, `users`.
Коммиты должны быть в загруженных ветках `origin`. Скрипт сам не подставляет `HEAD` или `main`.

В `pins.json` укажите соответствие исходных ссылок на образы и проверенных `sha256:...`.
Ключи берутся из `FROM` с учетом значений `ARG` в Dockerfile. Для запуска также нужны
`apache/kafka:4.3.1`, `postgres:17-alpine`, `cgr.dev/chainguard/minio:latest` и
`clamav/clamav:1.4_base`. Kafka UI относится к локальной разработке и в релиз не входит.
В манифесте сохраняются ссылки `image@sha256:...`, а не теги.

Временные файлы можно держать в `.local/`. Команды выполняются из `infra`:

```sh
python3 scripts/release_manifest.py plan --workspace .. \
  --sources .local/sources.json --pins .local/pins.json \
  --registry ghcr.io/overmindv --release pilot-1 --output .local/plan.json
python3 scripts/release_manifest.py build --workspace .. \
  --plan .local/plan.json --output .local/build.json
```

В план попадают SHA исходников, базовые образы и платформа `linux/amd64`. Для SQL-миграций
сохраняются путь, версия и SHA-256 файла; для схем API, GraphQL и Protobuf - путь и SHA-256.
Для JSON-схем также сохраняются идентификатор и версия, если они заданы в схеме.

Сборка повторно сверяет план с Git. Во временный контекст попадают файлы из `git archive`,
без локального `.env` и незакоммиченных правок. Базовые образы в рецепте сборки заменяются
на ссылки по digest. Скрипт не подставляет локальные настройки и секреты в build args.
Не храните секреты в самих репозиториях.

Поддерживаются обычные многостадийные Dockerfile, базовые образы из `FROM` и ссылки
на предыдущие стадии. Внешние образы в `COPY --from` и сторонний Dockerfile frontend
не допускаются.

## Опубликовать

Перед публикацией проверьте `plan.json`. Команда использует точные ID собранных образов
и сверяет их метки до первого push. Теги содержат имя релиза и ID образа.

```sh
make push RELEASE_BUILD=.local/build.json RELEASE_MANIFEST=.local/release.json
```

Загружайте релиз по ссылкам из `images` и `plan.upstream` в `release.json`. Образ каждого
сервиса с миграциями используется и для его контейнера `*-migrate`. Контейнер
`kafka-topics` использует образ Kafka, `media-buckets` использует образ MinIO.
Сам скрипт ничего не разворачивает и не запускает миграции.

Сохраните `release.json` вместе с артефактами поставки. Для повторной сборки можно
использовать вложенный `plan` и чистые клоны с теми же коммитами. План фиксирует исходники
и базовые образы, но не обещает побайтово идентичный результат, если Dockerfile обращается
к меняющимся репозиториям пакетов. Для повторной поставки используйте уже опубликованные
digest. После частично успешного push команду можно повторить с тем же `build.json`.
