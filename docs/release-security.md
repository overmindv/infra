# Проверка релиза перед выкладкой

Вызывайте **Release scan** только из приватного репозитория Overmindv. В публичном
`infra` публикуется код проверки, но не отчеты о приватных исходниках и образах.
Задача вызывающего workflow, после получения содержимого опубликованного `release.json`:

```yaml
scan:
  needs: candidate
  permissions:
    contents: read
    packages: read
  uses: overmindv/infra/.github/workflows/release-scan.yml@<infra SHA>
  with:
    manifest: ${{ needs.candidate.outputs.manifest }}
    scan_ref: '<infra SHA>'
  secrets:
    ORGANIZATION_READ_TOKEN: ${{ secrets.ORGANIZATION_READ_TOKEN }}
```

В обоих местах укажите один проверенный полный SHA infra. Workflow проверяет исходники
из записанных SHA и образы для `linux/amd64` по digest, включая Kafka, Postgres, MinIO
и ClamAV. Для образов сервисов он принимает только `ghcr.io/overmindv`.
`ORGANIZATION_READ_TOKEN` дает доступ на чтение нужных приватных репозиториев.
Для приватных образов GHCR дайте вызывающему репозиторию доступ на чтение пакетов.

Локальный запуск на Linux с Python 3.12+, Go 1.26, Node 24 и уже загруженными
репозиториями Overmindv:

```sh
bash scripts/install-release-scan-tools.sh "$HOME/.local/bin"
export PATH="$HOME/.local/bin:$PATH"
python3 scripts/release_scan.py scan --manifest .local/release.json \
  --workspace .. --output .local/release-security
```

Для приватных образов предварительно войдите в registry. Сканер не запускает контейнеры,
не публикует образы и не выполняет npm-скрипты. npm audit отправляет список зависимостей
в npm registry. Gitleaks проверяет зафиксированные исходники для сборки, а не всю историю Git.

## Когда не выкладывать

Предлагаемое правило до согласования политики с командой:

- Достижимые вызовы из отчета govulncheck блокируют выкладку, даже если severity не задан.
  Присутствие уязвимого модуля без найденного вызова остается в отчете.
- High и Critical в npm audit и образах блокируют выкладку до разбора. Сами эти сканеры не
  доказывают достижимость уязвимости, поэтому спорные случаи рассматриваются вручную.
- Совпадение правила секретов в исходниках или файловой системе образа блокирует выкладку.
  Значения совпадений не сохраняются в артефактах.
- Ошибка сканера, недоступный registry или неполный отчет не считаются чистой проверкой.

Исключения хранятся в `security/release-exceptions.json` приватного вызывающего
репозитория и проходят ревью как код. В публичном infra список остается пустым.
Для локальной проверки свой список можно передать через `--exceptions`.
Запись содержит `scanner`, `target`, `revision` (SHA исходников или digest образа),
`id`, `subject`, `owner`, `ticket`, `reason` и `expires` в формате `YYYY-MM-DD` UTC.
Используйте точные поля из отчета, Jira-задачу на разбор и срок до 30 дней. Исключение
не подходит к новому SHA или digest, не поддерживает маски и перестает действовать
с наступлением даты `expires`. Просроченные записи нужно удалить после разбора.

## Что сохраняется

Артефакт `release-security` содержит `report.json` с проверенными SHA, digest, итогом
каждого запуска, находками и примененными исключениями. `manifest_sha256` вычисляется
от JSON с отсортированными ключами, независимо от оформления входного файла. Для каждого
проверенного образа сохраняется CycloneDX SBOM. Рабочий каталог со сырыми отчетами не загружается.

Коды выхода: `0` - проверка пройдена, `1` - есть блокирующие находки, `2` - проверка
не завершилась или входные данные не прошли валидацию. Workflow не разворачивает релиз.

Форматы инструментов: [govulncheck](https://pkg.go.dev/golang.org/x/vuln/cmd/govulncheck),
[npm audit](https://docs.npmjs.com/cli/commands/npm-audit/),
[Trivy SBOM](https://trivy.dev/docs/latest/supply-chain/sbom/),
[Gitleaks](https://github.com/gitleaks/gitleaks).
