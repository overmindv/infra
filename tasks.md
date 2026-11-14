# План задач: infra

Дата анализа: **31 октября 2026**. Локальный HEAD: `2f2010b`; анализ также учитывает рабочее дерево на 2026-10-31.

## Текущее состояние

Есть локальный Compose, Kafka topics, миграции, сборка и smoke scripts; content/feed/sandbox уже присутствуют. Go-judge в локальном Compose privileged. Production CD отсутствует; dev ports и latest images нельзя автоматически считать production-конфигурацией.

Проверенные опорные файлы: `docker-compose.yml`, `Makefile`, `scripts/prepare-env.sh`, `tests/integration.sh`, `.github/workflows/ci.yml`.

## Правила выполнения

- Оценка по договорённости: **1 SP ≈ 1 рабочий день; 2 SP ≈ 2 рабочих дня одного разработчика**, включая проверку и review. Это предварительная оценка, не гарантия календарного срока.
- Сначала реализовать весь описанный в карточке функциональный срез, затем написать/обновить тесты последним шагом. Без TDD; общий e2e — после интеграции. Уже существующие проверки не выключать.
- Статус всех карточек: **запланировано**, код по этому backlog ещё не изменён. Наличие исходников и тестов не означает подтверждённую работоспособность.
- Обязательные зависимости указаны явно; рабочая очередь и расчёт срока — в [общем плане](../tasks.md). F0–F4 — технические этапы, а PILOT/PROD/LATER — границы выпуска.
- При смене контракта: согласовать производителя и потребителей, сгенерировать код, обеспечить совместимый rollout. Миграции только append-only; секреты/hidden tests/исходники решений в обычные логи не писать.
- Задачу больше двух дней после уточнения делить на отдельно принимаемые части, сохраняя ID родителя как ссылку. Не скрывать дополнительный объём внутри пункта «и всё остальное».

Всего: **14 задач / 21 SP**; обязательный срез: **9 задач / 12 SP**.

<a id="inf-01"></a>

## [infra] Select the low-cost pilot topology

- **ID:** INF-01
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F0 — решения и контракты; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [DOC-01](../docs/tasks.md#doc-01).
- **Где менять:** `docs/deployment.md (новый)`.
- **Готово, когда:** Есть конкретная схема пилота и список неизбежных single points of failure.

**Шаги:**

1. Спланировать один application host с Compose и отдельный изолированный runner host/VM.
2. Для 50 DAU оставить PostgreSQL/Kafka/S3-compatible storage без Kubernetes/Redis по умолчанию.
3. Записать расчёт RAM/disk, внешний backup, регион и бюджет; проверить стоимость провайдера перед заказом.
4. **Проверка после готовности функционала:** сверить capacity с памятью Kafka/ClamAV/Go builds и лимитами judge.

<a id="inf-02"></a>

## [infra] Add the new services to the local stack

- **ID:** INF-02
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F2 — пользовательские функции; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [REC-02](../recommendations/tasks.md#rec-02), [RAT-02](../rating/tasks.md#rat-02).
- **Где менять:** `docker-compose.yml`, `Makefile`, `scripts/prepare-env.sh`.
- **Готово, когда:** Полный учебный сценарий запускается одной документированной командой.

**Шаги:**

1. Добавить recommendations/rating с отдельными БД, migrate jobs и service tokens.
2. Создать согласованные learning/rating event topics и ACL mapping.
3. Включить healthchecks, local env placeholders и test commands.
4. **Проверка после готовности функционала:** compose config и health smoke без вывода секретов.

<a id="inf-03"></a>

## [infra] Create a production Compose overlay

- **ID:** INF-03
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F1 — надёжная основа; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [INF-01](#inf-01).
- **Где менять:** `deploy/compose.production.yml (новый)`.
- **Готово, когда:** Внешний доступ только к web/TLS, runner отделён от application network.

**Шаги:**

1. Закрыть наружу БД, брокер, внутренние API, admin UI и go-judge.
2. Добавить resource limits/restart/log rotation/non-root где поддерживается.
3. Разделить local seeds и production migrations; pin images по digest.
4. **Проверка после готовности функционала:** network reachability и отсутствие demo accounts.

<a id="inf-04"></a>

## [infra] Configure HTTPS and service credentials

- **ID:** INF-04
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F1 — надёжная основа; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [INF-03](#inf-03), [GW-01](../api-gateway/tasks.md#gw-01).
- **Где менять:** `deploy`, `scripts`.
- **Готово, когда:** Cookie Secure работает; внутренние actor headers нельзя прислать напрямую в сервис.

**Шаги:**

1. Использовать Caddy либо существующий nginx с автоматическим TLS на выбранном хосте.
2. Создать отдельные credentials/DB roles per service, root-only secret files и ротацию.
3. Разрешить browser presigned upload только к нужному object-storage origin.
4. **Проверка после готовности функционала:** TLS/CORS, spoofed headers и secret-redaction checks.

<a id="inf-05"></a>

## [infra] Provision versioned event topics

- **ID:** INF-05
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F1 — надёжная основа; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [DOC-03](../docs/tasks.md#doc-03), [TSK-01](../tasks/tasks.md#tsk-01).
- **Где менять:** `docker-compose.yml`, `scripts`.
- **Готово, когда:** Переход контракта не теряет queued submissions.

**Шаги:**

1. Добавить/сверить request/result/retry/DLQ и learning/contribution/account topics.
2. Настроить retention, max message size и ACL producer/consumer.
3. При смене execution schema обеспечить coexistence v1/v2 и drain старых сообщений.
4. **Проверка после готовности функционала:** produce/consume права и schema fixtures в тестовом broker.

<a id="inf-06"></a>

## [infra] Build immutable release manifests

- **ID:** INF-06
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F1 — надёжная основа; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** нет; можно брать после согласования границ релиза.
- **Где менять:** `scripts/build-images.sh`, `scripts/push-images.sh`, `.github/workflows`.
- **Готово, когда:** Одинаковый release можно воспроизвести и откатить.

**Шаги:**

1. Собрать manifest точных SHA всех repo и digest images, включая текущий changed repo.
2. Создавать release без latest/main drift и без автоматического push developer WIP.
3. Сохранять schema versions и migration versions рядом с manifest.
4. **Проверка после готовности функционала:** build/checkout по manifest в чистой директории.

<a id="inf-07"></a>

## [infra] Restore the shared integration pipeline

- **ID:** INF-07
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F2 — пользовательские функции; **контур:** PROD — дополнение к полному продакшену.
- **Приоритет:** P2 — последующее улучшение.
- **Зависит от:** [INF-06](#inf-06).
- **Где менять:** `../tests/.github/workflows/e2e.yml`, `.github/workflows`.
- **Готово, когда:** CI проверяет именно публикуемый набор версий в пределах выбранного бюджета.

**Шаги:**

1. Исправить checkout feed/sandbox и новые services через release manifest.
2. Убрать игнорирование ошибки prepare-env и shared credentials fallback.
3. Для двух разработчиков запускать полный stack перед release, дешёвые unit checks на PR.
4. **Проверка после готовности функционала:** ручной staging pipeline и artifact inspection.

<a id="inf-08"></a>

## [infra] Deploy a repeatable staging environment

- **ID:** INF-08
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F3 — выпуск и эксплуатация; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [INF-02](#inf-02), [INF-04](#inf-04), [INF-05](#inf-05), [INF-06](#inf-06).
- **Где менять:** `deploy`, `.github/workflows`.
- **Готово, когда:** Staging повторяет release topology без копирования личных данных.

**Шаги:**

1. Поднять staging теми же images/config templates с отдельными данными и секретами.
2. Добавить deploy script migrate→start→readiness→smoke.
3. Миграции запускаются одним job, приложение не получает schema-owner роль.
4. **Проверка после готовности функционала:** fresh deploy и upgrade существующей БД.

<a id="inf-09"></a>

## [infra] Add a safe deployment rollback path

- **ID:** INF-09
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F3 — выпуск и эксплуатация; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [INF-08](#inf-08).
- **Где менять:** `deploy`, `.github/workflows`.
- **Готово, когда:** Откат не требует стирать volume и не ломает ожидающие события.

**Шаги:**

1. Развернуть release по manifest с preflight backup/compatibility checks.
2. При failed smoke вернуть предыдущие images без destructive down migration.
3. Expand-and-contract migration проверять с предыдущим приложением.
4. **Проверка после готовности функционала:** failed rollout drill с queued submission.

<a id="inf-10"></a>

## [infra] Back up databases and private objects

- **ID:** INF-10
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F1 — надёжная основа; **контур:** PILOT — обязательный срез.
- **Приоритет:** P1 — обязательная функция.
- **Зависит от:** [INF-03](#inf-03), [DOC-05](../docs/tasks.md#doc-05).
- **Где менять:** `scripts/backup`, `deploy (новые)`.
- **Готово, когда:** Потеря основного диска не уничтожает весь проект.

**Шаги:**

1. Для малого пилота настроить зашифрованные регулярные backups всех service DB и private storage вне основного хоста.
2. Хранить manifest, checksums и retention; ключ восстановления хранить отдельно.
3. Отдельно сохранить нужные private suites и policy artifacts, Kafka не считать единственной копией business data.
4. **Проверка после готовности функционала:** restore в изолированное окружение и сверка БД+объектов.

<a id="inf-11"></a>

## [infra] Monitor core service health and queues

- **ID:** INF-11
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F3 — выпуск и эксплуатация; **контур:** PROD — дополнение к полному продакшену.
- **Приоритет:** P2 — последующее улучшение.
- **Зависит от:** [TSK-14](../tasks/tasks.md#tsk-14), [REC-25](../recommendations/tasks.md#rec-25).
- **Где менять:** `observability (новый)`.
- **Готово, когда:** Два разработчика видят сбой до жалобы пользователя.

**Шаги:**

1. Развернуть минимальные Prometheus/Grafana либо более лёгкий эквивалент по RAM budget.
2. Настроить alerts latency/error/queue age/disk/backup age и внешний uptime probe.
3. Закрыть dashboards и проверить отсутствие high-cardinality labels.
4. **Проверка после готовности функционала:** искусственный failed backup и stopped worker alert.

<a id="inf-12"></a>

## [infra] Scan release artifacts and dependencies

- **ID:** INF-12
- **Оценка:** 1 SP (1 рабочий день)
- **Этап:** F3 — выпуск и эксплуатация; **контур:** PROD — дополнение к полному продакшену.
- **Приоритет:** P2 — последующее улучшение.
- **Зависит от:** [INF-06](#inf-06).
- **Где менять:** `.github/workflows`.
- **Готово, когда:** Релиз имеет проверяемый отчёт, а не обещание отсутствия уязвимостей.

**Шаги:**

1. Добавить govulncheck, npm dependency audit и container scan по lockfiles/digests.
2. Сформировать SBOM и проверку секретов с allowlist исключений и сроком.
3. Блокировать exploitable critical/high по согласованной triage policy.
4. **Проверка после готовности функционала:** fixture vulnerable image/secret и clean build.

<a id="inf-13"></a>

## [infra] Run a bounded capacity and recovery drill

- **ID:** INF-13
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F3 — выпуск и эксплуатация; **контур:** PROD — дополнение к полному продакшену.
- **Приоритет:** P2 — последующее улучшение.
- **Зависит от:** [INF-08](#inf-08), [SBX-07](../sandbox/tasks.md#sbx-07), [TST-08](../tests/tasks.md#tst-08).
- **Где менять:** `tests`, `docs/capacity.md (новый)`.
- **Готово, когда:** Есть подтверждённый лимит пилота и процедура отказа сверх него.

**Шаги:**

1. Измерить реальный workload пилота: 50 DAU, небольшой burst web и ограниченные concurrent code jobs.
2. Проверить потерю broker, перезапуск DB, заполнение disk и bounded recovery.
3. Записать p95, backlog, RAM/CPU и честные RTO/RPO по измерению.
4. **Проверка после готовности функционала:** k6/load fixtures и восстановление контрольного решения/плана.

<a id="inf-14"></a>

## [infra] Prepare production scale-out options

- **ID:** INF-14
- **Оценка:** 2 SP (2 рабочих дня)
- **Этап:** F4 — развитие после данных/нагрузки; **контур:** LATER — после данных/роста.
- **Приоритет:** P2 — последующее улучшение.
- **Зависит от:** [INF-13](#inf-13).
- **Где менять:** `docs/scaling.md (новый)`.
- **Готово, когда:** Масштабирование имеет пороги и стоимость, не блокирует диплом.

**Шаги:**

1. По измеренным bottlenecks рассчитать дополнительные runner workers, DB и broker replicas.
2. Оценить managed storage/DB и Kubernetes только при необходимости эксплуатации.
3. Описать миграцию без потери event history и cost trigger.
4. **Проверка после готовности функционала:** проверить capacity model на pilot measurements.
