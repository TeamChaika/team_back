# Сбор iiko, фоновые задания и данные

Срез: 2026-10-03, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`. Публичный портал — [app/serve.py](../../app/serve.py) → [app/portal.py](../../app/portal.py). Приватный сборщик — отдельный [app/main.py](../../app/main.py): [app/api/router.py](../../app/api/router.py) → `app/api/routes/*` → [app/api/dependencies.py](../../app/api/dependencies.py) → `app/services/*` → [app/integrations/iiko/client.py](../../app/integrations/iiko/client.py). Контракт ответа расположен в `app/schemas/*`.

| Данные/процесс | Где начать |
| --- | --- |
| Подключения и лицензия iiko | [app/services/iiko_auth.py](../../app/services/iiko_auth.py), [app/services/iiko_connections.py](../../app/services/iiko_connections.py); приватный маршрут [iiko_connections.py](../../app/api/routes/iiko_connections.py) |
| Структура и RAW | [app/sync_references.py](../../app/sync_references.py): `configured_sources`, `reference_lock`, `append_snapshot`, `corporate_nodes`, `stores`, `rms_bindings` |
| Номенклатура и техкарты | [app/sync_inventory.py](../../app/sync_inventory.py) → `products`, `assembly_charts`, `assembly_chart_items/scopes` |
| Накладные/списания/перемещения | [app/sync_invoices.py](../../app/sync_invoices.py), [app/sync_outgoing.py](../../app/sync_outgoing.py), [app/sync_transfers.py](../../app/sync_transfers.py), [app/sync_writeoffs.py](../../app/sync_writeoffs.py) → документы и позиции `chaika.*` |
| OLAP-история | [app/sync_sales_history.py](../../app/sync_sales_history.py) → [app/import_sales_review.py](../../app/import_sales_review.py) → `sales_report_sets/days/reports/rows`; публикация дня учитывает проверку и покрытие |
| Смены и остатки | [app/sync_cash_shifts.py](../../app/sync_cash_shifts.py), [app/sync_cash_shift_history.py](../../app/sync_cash_shift_history.py), [app/sync_store_balances.py](../../app/sync_store_balances.py), [app/sync_counteragent_balances.py](../../app/sync_counteragent_balances.py) |
| Сотрудники/справочники | [app/sync_employees.py](../../app/sync_employees.py), [app/sync_employee_roles.py](../../app/sync_employee_roles.py), [app/sync_dictionaries.py](../../app/sync_dictionaries.py), [app/sync_accounts.py](../../app/sync_accounts.py) |
| События RMS | [app/sync_events.py](../../app/sync_events.py), [app/sync_event_history.py](../../app/sync_event_history.py), [app/event_storage.py](../../app/event_storage.py) → версии, наблюдения и дни событий |
| Фильтры показателей | [app/sync_indicator_filters.py](../../app/sync_indicator_filters.py) сохраняет варианты отдельно от онлайн-запросов показателей; аналитическая карта — [analytics.md](analytics.md) |
| Cron | [app/scheduler.py](../../app/scheduler.py) при включённом `sync_enabled` запускается порталом, поднимает loopback-сборщик, выбирает слоты и пишет `chaika.scheduled_sync_runs`; сбой получает retry через 5 минут |
| Ручной запуск задачи расписания | `/api/status/sync/{job}/run` в [app/portal.py](../../app/portal.py) → [Repository.manual_sync/status](../../app/web/repository.py) → [app/manual_sync.py](../../app/manual_sync.py); durable очередь, общий 10-минутный лимит, heartbeat; исполняет существующий scheduler между cron-задачами. [Контракт и выпуск](../scheduled-sync.md#ручной-запуск-отдельных-задач), [миграция](../../supabase/migrations/20261004120000_manual_scheduled_sync.sql) |
| Ручной refresh | [app/api/routes/sync_jobs.py](../../app/api/routes/sync_jobs.py) → [app/services/sync_jobs.py](../../app/services/sync_jobs.py) → [app/sync_refresh.py](../../app/sync_refresh.py); manifest 60-дневного запроса в `.local/sync`, отдельно от таблицы cron |

Миграции `supabase/migrations/*`: [reference_sync.sql](../../supabase/migrations/20260910171101_chaika_reference_sync.sql) задаёт RAW/RLS и опорные таблицы; [inventory_sync.sql](../../supabase/migrations/20260910172520_chaika_inventory_sync.sql) — товары, техкарты, документы; [portal_and_olap.sql](../../supabase/migrations/20260911013009_chaika_portal_and_olap.sql) — OLAP; [scheduled_sync.sql](../../supabase/migrations/20260915123000_scheduled_sync.sql) — cron; [indicator_filters.sql](../../supabase/migrations/20260918190000_indicator_filters.sql) — варианты фильтров. Схема нативных документов отдельна: [documents.md](documents.md).

Tenant OLAP bootstrap (09.10.2026): `approved_templates` сначала ищет reviewed набор
в собственной схеме. Если его нет, только runtime `tenant` использует общий кодовый
контракт `tenant-sales-v1`: базовый `DailySalesQuery` и группировки семи аналитических
отчётов в `tenant_sales_templates`. Это определения запросов без RAW, чужих данных
или переноса approval; `publish` сохраняет обычный `reviewed=false`. Такой контракт
остаётся доступен при следующих запусках до собственного reviewed набора. Неполный
или неверный reviewed набор вызывает ошибку; legacy runtime без reviewed набора
по-прежнему блокируется. Каждая загрузка получает свой fingerprint, RAW и manifest,
подтверждённый logout; `parse_review` проверяет источник, хэши, поля/метрики и период
перед транзакцией `publish`. Расхождения reconciliation остаются в `checks` и
`warning_days`, как в обычной истории, и не превращаются в точное совпадение.

Standalone tenant (10.10.2026): `sync_references.reference_mode` принимает настоящий
`STANDALONE_RMS` только как единственный `primary`; режим `CHAIN` с отдельными
`REPLICATED_RMS` сохраняется. Тип читается из enum ответа `replication/serverType`
(plain text, JSON string или строгий XML `serverType`), а не из URL. Standalone
однократно загружает собственные departments/groups/stores и сопоставляет UUID
групп с собственным подразделением; replication не запрашивается, её неприменимость
отмечается явно без искусственных RAW. Миграция
`20261010160000_tenant_standalone_primary.sql` разрешает только подтверждённую
`primary → primary` связь с одинаковым departments snapshot и сохранённым типом.

`source_capabilities.require_primary_source` проверяет standalone configured,
verified_at, matched self-binding и отсутствие других настроенных источников
в БД и текущем manifest. Эту проверку используют девять primary-загрузчиков
и selector событий `sync_events.event_sources`. События standalone хранятся
под настоящим `primary`; capture отдельно проверяет живой serverType и освобождает
сессию. Resume истории использует обычные даты покрытия и не делает повторную
выгрузку завершённого дня. Полная initial_sync, ACL и feature readiness не ослабляются.
Тесты: `test_standalone_primary.py`, `test_inventory_sync.py`, tenant migration suite;
реальная загрузка iiko подтверждается отдельно от синтетических HTTP/PG проверок.

[CI](../../.github/workflows/ci.yml) запускает Ruff, изолированный PostgreSQL через [prepare_test_database.py](../../tools/prepare_test_database.py), pytest и smoke контейнера. [Dockerfile](../../Dockerfile) собирает публичный портал. Операционные точки входа и ограничения размещения — [operations.md](operations.md). Тесты синхронизации: [test_reference_sync.py](../../tests/test_reference_sync.py), [test_sales_import.py](../../tests/test_sales_import.py), [test_scheduler.py](../../tests/test_scheduler.py), [test_sync_jobs.py](../../tests/test_sync_jobs.py).
