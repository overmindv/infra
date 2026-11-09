# Рекомендации практики

В Compose добавлен внутренний сервис `recommendations`; публичного host port нет.
Gateway вызывает его по `RECOMMENDATIONS_URL`, общий service token хранится в `.env`.
`make init`/`scripts/prepare-env.sh` создаёт `RECOMMENDATIONS_SERVICE_TOKEN`, в том числе
при обновлении существующего `.env`. Значения токена не выводить и не коммитить.

Compose поднимает отдельную `recommendations-postgres` и одноразовый
`recommendations-migrate`. БД хранит выбранные темы и бюджет занятия каждого
пользователя; preview строится по актуальной bounded metadata/history из gateway.
Kafka, Redis, GPU и внешний AI API не требуются. Сохранённая цель не является
постоянной учебной моделью или доказательством освоения.
При запуске всего стека нужен sibling checkout `../recommendations`.
Frontend страница `/practice` требует входа. Нужны активные темы и опубликованные
задачи с привязкой к темам. Пустой каталог честно даёт пустую подборку.

Проверки без запуска стека:

```bash
make lint
make agent-check STRICT=1
make agent-verify SERVICE=recommendations
make recommendations-contract
```

Запуск локального стека остаётся `make up`. Railway этим изменением не создаётся
и не настраивается. Оценка ресурсов: `../docs/architecture/recommendations.md`.

`recommendations-contract` собирает два Go binary и запускает их на локальных временных
портах с synthetic upstream. Проверяет GraphQL/HTTP, actor isolation, бюджет, историю
и безопасные отказы. Без БД проверяет preview. Если задана
`CONTRACT_GOALS_TEST_DSN` с **выделенной тестовой БД** и заранее применённой
миграцией, дополнительно проверяет сохранение, чтение, изоляцию и удаление цели.
Это не e2e с настоящими tasks/entities. Требует Go; не читает `.env`.
