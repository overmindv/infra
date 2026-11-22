# Kafka: топики и права доступа

`kafka-topics` в локальном Compose создает топики до старта приложений.
Каталог и план можно проверить без подключения к брокеру:

```sh
sh scripts/kafka/topics.sh plan
```

| Поток | Кто пишет | Кто читает |
| --- | --- | --- |
| `code-execution.requests.v1` | Tasks | Sandbox |
| `code-execution.results.v1` | Sandbox | Tasks |
| `code-execution.retry.v1` | Sandbox | Sandbox |
| `code-execution.events.v1`, `code-execution.deadletter.v1` | Sandbox | Сервисным аккаунтам чтение не выдается |
| `catalog.activity.v1`, `task.activity.v1` | Entities, Tasks | Feed |
| `content.content-item-events` | Content | Нет подключенного потребителя |
| `learning.outcome.v1`, `content.learning.v1` | Tasks, Content | Recommendations, группа `recommendations-learning-evidence-v1` |
| `learning.goal.v1` | Recommendations | Право чтения не выдается до подключения Rating |
| `content.material.v1` | Content | Feed (`feed-v1`), право чтения также зарезервировано для Recommendations |
| `content.contribution.v1` | Content | Пока нет потребителя |
| `user.deleted.v1` | Зарезервирован | ACL нет до согласования контракта Users |

По умолчанию срок хранения новых топиков 7 суток, partitions 3, replication
factor 1. Лимит Kafka относится к **пакету записей**, а не к размеру JSON:
3 МиБ для execution, 256 КиБ для новых метаданных, 1048588 байт для старых
activity и content. Sandbox отдельно проверяет распакованный запрос до
2 МиБ. Клиенты могут иметь меньшие лимиты. Для увеличения лимита проверьте
producer, consumer и репликацию тем же несжимаемым payload.

Скрипт пропускает существующие топики и не меняет их настройки или offsets.
Он не сокращает retention, не пересоздает топики и не увеличивает partitions
без отдельной миграции. Изменение числа partitions может нарушить порядок
по ключу.

## Брокер с ACL

Применяйте режим `secure` с Kafka CLI той же версии, что у брокера.
Нужны `StandardAuthorizer`, `allow.everyone.if.no.acl.found=false`,
отдельные аутентифицированные principals и TLS. Передайте путь к
admin properties из закрытого файлового монтирования, а не пароль в CLI:

```sh
sh scripts/kafka/topics.sh secure kafka.internal:9092 /run/secrets/kafka-admin.properties
```

Режим сверяет topic settings и останавливается при расхождении.
Права добавляются только для известных `User:tasks`, `User:sandbox`,
`User:entities`, `User:feed`, `User:content` и `User:recommendations`.
Writer получает `Write` на свой топик, reader получает `Read` на топик и
свою группу, если она нужна. Нет разрешений на создание или удаление
топиков для сервисов. Действующие чужие ACL не удаляются: перед запуском
проверьте разрешения на топики и группы, включая wildcard и prefix grants.
На локальном брокере аутентификация и ACL не включаются автоматически.
Для выката еще нужны клиентские credentials по OVM-163.

## Публикации в ленте

1. Примените миграции Content и Feed. Сверьте ACL на `content.material.v1`.
2. Укажите `FEED_MATERIAL_ACTIVITY_TOPIC` с тем же именем, что в
   `KAFKA_CONTENT_MATERIAL_TOPIC`. Включите `CONTENT_ACTIVITY_ENABLED=true`.
3. С выключенными карточками проверьте потребление событий публикации и
   архивации, позиции группы и отсутствие необработанных ошибок. Только затем
   задайте `FEED_MATERIAL_CARDS_ENABLED=true`.

Не выключайте публикацию событий, пока карточки видны читателям. В Feed не
подключаются потоки учебных подтверждений и редакторского вклада.
Изменение не переносит старые публикации в ленту само по себе.

## Переход на v2

1. После согласования независимой схемы проверьте план с
   `KAFKA_RESERVE_EXECUTION_V2=true`. Скрипт создаст еще пять топиков v2,
   с отдельными principals и группами. Это не включает codecs и не
   меняет маршрутизацию приложений.
2. Запустите совместимые читатели v2 прежде, чем переключать новые заявки.
   Закрепите версию топика за запуском: повтор, результат и DLQ старой
   заявки остаются в v1. Не пускайте v1 JSON в v2 ради переключения.
3. Оставьте v1 consumers, пока не опустеют отложенные повторы и outbox,
   не завершатся активные попытки и не станет нулевым lag **по каждой**
   partition запросов, повторов и результатов. Пустой ответ о группе
   не считается нулевым lag. Разберите DLQ и повторите сверку после
   максимальной задержки retry. Не сбрасывайте offsets.
4. Если v2 сломалась, верните новые заявки в v1. Оставьте читателей v2
   обработать уже принятые заявки. Удаление старых топиков не входит
   в этот переход.

Проверки: `sh tests/kafka-topics-plan.sh` и
`TASKS_FIXTURES_DIR=<каталог execution-contract> sh tests/kafka-acl-live.sh`.
Живой тест поднимает Kafka без опубликованных портов, отправляет fixtures
в v1 и отдельный контрольный пакет в v2. Он проверяет изоляцию версий,
а не совместимость будущего формата v2.
