# Мониторинг Overmindv

`compose.observability.yml` добавляет Prometheus, Alertmanager и экспортер
файловых метрик. Графики, Targets и Alerts доступны в Prometheus. Grafana,
Docker socket и доступ контейнеров к данным сервисов не нужны.

Лимит контейнеров суммарно 304 МиБ. Блоки Prometheus хранятся до 7 дней,
с ограничением 512 МБ. Оставьте место также под WAL, служебные файлы и
данные Alertmanager. Пороги ниже стартовые, они не заменяют SLO и RPO.

## Запуск на тестовом стенде

1. Создайте `.local/monitoring/{prometheus,alertmanager,targets,signals}`
   на Linux-хосте с Docker. Другой каталог задается через
   `MONITORING_STATE_DIR`. Для `prometheus` и `alertmanager` используйте
   права `0750`, для секретных файлов `0640`; владелец оператор,
   группа с GID 65534 для чтения контейнерами. `targets` и `signals`
   должны быть доступны экспортеру и Prometheus на чтение, без секретов.
2. В `prometheus/web.yml` добавьте `basic_auth_users:` и отдельную учетную
   запись с bcrypt-хешем для каждого разработчика. Хеши можно получить
   командой `htpasswd -nBC 12 <имя>`; перенесите пары в YAML с пробелом
   после двоеточия. Не передавайте пароль аргументом процесса.
3. В `alertmanager/webhook-url` укажите HTTPS URL приемника формата
   Alertmanager webhook без токена в адресе. Bearer-токен положите
   отдельно в `alertmanager/webhook-token`. Приемник должен уведомлять обоих разработчиков
   о сбое и восстановлении. Проверьте доставку до включения дежурства.
4. Задайте `MONITORING_PROMETHEUS_IMAGE` и `MONITORING_ALERTMANAGER_IMAGE`
   в `.env` с digest проверенных образов и запустите оверлей:

   ```sh
   docker compose -f docker-compose.yml -f compose.observability.yml \
     up -d prometheus alertmanager monitoring-signals
   ```

Prometheus слушает `127.0.0.1:9097`. Для удаленного просмотра используйте
SSH-туннель: `ssh -L 9097:127.0.0.1:9097 <хост>`. Не публикуйте
Prometheus, Alertmanager и сервисные `/metrics` через внешний ingress.
Порты Alertmanager и экспортера на хост не пробрасываются.

## Перед выкатом

Образы из `tests/monitoring-images.env` предназначены для изолированных
тестов: Prometheus 3.16.0 и Alertmanager 0.35.0 собраны с Go 1.27.3.
Для прода нужны сборки с исправлением GO-2026-6859. Задайте проверенные
образы с digest через `MONITORING_PROMETHEUS_IMAGE` и
`MONITORING_ALERTMANAGER_IMAGE`; повторите проверки перед запуском.
Экспортер в оверлее рассчитан на linux/amd64. Для другой архитектуры
укажите соответствующий образ через `MONITORING_SIGNALS_IMAGE`.

### Метрики отдельных сервисов

- Если worker включен, используйте `observability/targets/sandbox.example.yml`
  как `targets/sandbox.yml`. Отсутствующий файл означает, что worker не
  ожидается; остановка ожидаемого worker дает `MonitoringTargetDown`.
- После выката OVM-254 включите `targets/tasks.yml` по образцу и передайте
  Tasks `TASKS_EXECUTION_METRICS_TOKEN`. Тот же токен нужен Prometheus в
  `prometheus/tasks-token`. После выката OVM-220 аналогично подключите
  `targets/recommendations.yml`, `RECOMMENDATIONS_STUDY_METRICS_TOKEN`
  и `prometheus/recommendations-token`.
- Пересоздайте измененный сервис с оверлеем и проверьте его Target.
  Токены разные, по 32 и более случайных символа, не добавляются в Git
  и передаются только сервису-владельцу и Prometheus.

## Диск, бэкап и внешняя проверка

Раз в минуту запускайте с хоста `sh observability/signals.sh <signals-dir> disk <data-path>`.
Укажите путь на том же диске, где лежат рабочие данные Docker. Если база
и объекты разнесены по дискам, настройте контроль каждого отдельно.
Отметка старше 3 минут или ошибка чтения файла дают `HostDiskSampleStale`.

Задача резервного копирования вызывает
`sh observability/signals.sh <signals-dir> backup ok` после записи и
проверки удаленной копии, либо `backup failed` при ошибке. Отметка успеха
сохраняется при неудачной попытке. Запуски бэкапа не должны пересекаться.
Нет отметки или последняя проверенная копия старше 26 часов дает
`BackupStale`. Мониторинг не создает и не проверяет копии сам.

С независимого хоста или CI вне того же VPS раз в минуту проверяйте
публичный HTTPS endpoint:

```sh
sh observability/uptime.sh https://<публичный-хост>/health <файл-с-ping-url>
```

Файл `0600` содержит один HTTPS URL Healthchecks или совместимого
приемника. Скрипт посылает heartbeat при HTTP 2xx и `/fail` при ошибке;
редиректы не считаются успехом. В приемнике настройте период 1 минута,
допуск на задержку и уведомления обоих разработчиков. Пропуск heartbeat
ловит и отказ самого проверяющего хоста.

## Разбор алерта

- `MonitoringTargetDown`: проверьте порт, сеть, токен и статус контейнера
  на странице Targets. `CoreServiceNotReady` отличает запущенный процесс
  от сервиса, который не может обслуживать запросы.
- `ExecutionQueueOldest`: `outbox > 60s` указывает на очередь публикации;
  `result > 240s` при пустом outbox требует проверить worker и результат
  в Kafka. При `ExecutionQueueSamplerDown` показания возраста устарели.
- `CoreHTTPErrorRate` и `CoreHTTPLatency`: сверьте 5xx и p95 по известным
  маршрутам за 5 минут. При малом трафике эти правила не срабатывают.
- `HostDiskLow`: меньше 15% свободного места. Найдите источник роста,
  прежде чем освобождать место. Не удаляйте тома БД.
- `BackupFailed`: проверьте последний запуск и доступ к месту хранения.
  Сверьте возраст предыдущей подтвержденной копии. Не выставляйте успех
  вручную ради отключения алерта.
- `AlertDeliveryFailed`: проверьте доступ Alertmanager к приемнику и его
  ответ. Этот алерт может быть виден только в Prometheus, поэтому внешняя
  проверка использует отдельный канал.

Перед коммитом: `sh scripts/check-monitoring.sh`. Для приемки на тестовом
стенде можно запустить `sh tests/monitoring-live.sh` (Docker, curl, Python 3
и Perl с bcrypt). Тест поднимает отдельный Compose-проект без рабочих БД.
На общем тестовом стенде остановите ожидаемый worker и запишите
`backup failed` в тестовый каталог сигналов. Должны прийти два
уведомления. После запуска worker и записи подтвержденного успеха
должны прийти уведомления о
восстановлении. Не имитируйте сбой в каталоге рабочих бэкапов.
