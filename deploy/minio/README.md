# Сверка Media с MinIO

`media-inventory.json` нужна только для ручного запуска `media-reconcile`.
Политика разрешает список ключей в `media/originals/`, `media/variants/`,
`media/public/verified/variants/` и `media-quarantine/uploads/`.
Она не выдает доступ к содержимому объектов
и не разрешает запись или удаление.

Оператор назначает политику отдельной временной учетной записи. Compose
и `init.sh` не создают эту запись и не передают ее ключи в Media.
Для резервных копий, восстановленных под другими именами, используйте ту же
политику с точными именами тестовых бакетов.

Пользователь PostgreSQL для сверки должен иметь только доступ к БД, схеме
и `SELECT` на `files`, `blobs`, `blob_variants`, `upload_sessions`
и `media_job_objects`.
