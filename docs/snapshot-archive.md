# Архив RAW iiko в S3

`app/snapshot_archive.py` хранит исходный ответ и normalized JSON одним gzip envelope
версии 1. В PostgreSQL остаются прежние UUID, источник, ресурс, время, размер и SHA
исходного ответа, а также `payload_object_*`, версия формата и время проверки.
Нормализованные бизнес-таблицы и их внешние ключи сохраняются.

Primary: `CHAIKA_SNAPSHOT_ARCHIVE_MODE=disabled|s3|broker` (по умолчанию disabled).
Для s3 нужны переменные с тем же префиксом `ENDPOINT` (точный HTTPS endpoint),
`BUCKET`, `REGION`, `ACCESS_KEY`, `SECRET_KEY`, `SCOPE`.
Scope — `chaika` или `c_<32 lowercase hex UUID>_analytics`.
Для broker нужен абсолютный `BROKER_SOCKET`.
Секреты передаются только закрытой конфигурацией процесса, не через CLI/Git.

Tenant runtime использует только `RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_MODE`
(disabled/broker) и `RESTCONTROL_TENANT_SNAPSHOT_ARCHIVE_BROKER_SOCKET`;
scope выводится из проверенного runtime. Прямой S3 и ambient primary credentials
в tenant процессе отвергаются.

Ключ: `restcontrol/snapshots/v1/{scope}/{snapshot UUID}/{object SHA256}.json.gz`.
Gzip детерминирован (`mtime=0`); SHA объекта относится к сжатым байтам.
Envelope содержит version, scope, id, source_id, resource, sha256, source_bytes,
raw_base64 и normalized. До INSERT выполняется PUT → GET, проверка размера/SHA
объекта, всех полей идентичности и SHA исходного ответа. Ошибка останавливает
публикацию; автоматического возврата к inline нет. Неудачный INSERT после PUT
может оставить объект без ссылки: повтор использует тот же детерминированный ключ.

Лимиты: 128 MiB сжатого объекта, 512 MiB распакованного envelope; превышение
останавливает обмен с фиксированным кодом ошибки. S3 timeout: connect 10s/read 60s,
ограниченные retries; proxy environment не используется. Broker client timeout 90s.
Исходные credential-redaction правила employee/events применяются до архивации.

Сначала применяется `20261017140000_snapshot_archive.sql`, затем выпускается код.
Disabled сохраняет прежний INSERT и совместим со старой схемой. Inventory resume
читает новые колонки и требует миграцию; поддерживаются inline и external snapshots.
Для новых/существующих tenants нужна отдельная добавочная tenant migration.
Не изменять baseline/checksum ранее применённых миграций.

Backfill выполняется отдельным операторским инструментом владельцем схемы.
Runtime сохраняет только SELECT/INSERT. Удалять snapshot rows нельзя из-за FK.
Освобождение payload не равно немедленному уменьшению файлов PostgreSQL:
измерять TOAST, cleanup horizon, WAL и свободное место после ограниченных партий.
Rollback отключает новые external записи; чтение уже архивированных payload
требует доступного storage и корректной конфигурации.

Отдельные `sales_report_captures`, legacy `sales_reports.raw`,
`sales_drilldown_captures` и локальные collector файлы этой миграцией не меняются.

Проверки: `python -m pytest tests/test_snapshot_archive.py -q`.

Операторский CLI запускается из корня репозитория:

```sh
python -m tools.archive_snapshots --schema chaika --dsn-file /private/db.json --manifest /private/archive.jsonl --limit 1
python -m tools.archive_snapshots --schema chaika --dsn-file /private/db.json --manifest /private/archive.jsonl --limit 1 --apply --guard-file /private/disk-guard.json
```

Без `--apply` загружает и проверяет S3, сохраняя inline payload. `--dsn-file` —
закрытый JSON с psycopg connect parameters, права не шире 0600, без symlink.
Manifest дописывается построчно, flush+fsync после результата; содержит UUID,
метаданные и указатель, без payload/credentials. Повтор выбирает только inline rows.
`--workers` 1–4, по умолчанию 1; держать минимальный размер партии при нехватке диска.

Отдельный наблюдатель должен обновлять guard JSON: `checked_at` (Unix seconds),
`available_bytes` (свободное место на диске PostgreSQL). Максимальный возраст 30s;
`--minimum-free-bytes` по умолчанию 768 MiB, запрещено ниже 512 MiB.
Проверка выполняется перед запуском партии и непосредственно перед UPDATE.
UPDATE сверяет PostgreSQL JSONB с восстановленным normalized: потеря точности
JSON numeric, изменение строки или ошибка fetch сохраняют inline.
После миграции проверить constraint отдельной транзакцией владельца:

```sql
ALTER TABLE chaika.raw_snapshots VALIDATE CONSTRAINT raw_snapshots_payload_storage_check;
```

PostgreSQL-проверки используют только disposable `127.0.0.1:15438`:
`tests/test_snapshot_archive_backfill.py` проверяет реальный FK склада,
сохранение metadata, numeric semantic mismatch, guard/fetch failure и CHECK.
