# Карта backend Chaika Team

Срез кода: **2026-10-03**, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`.
Карта помогает выбрать нужный модуль; она не подтверждает текущую конфигурацию production.

| Задача | Тематическая карта | Первый файл |
| --- | --- | --- |
| Вход, сессия, рестораны, разделы | [API и доступ](CODEMAPS/api-access.md) | [app/portal.py](../app/portal.py) |
| Мой профиль, свой пароль, подключить Telegram | [API и доступ](CODEMAPS/api-access.md) | [app/web/profile.py](../app/web/profile.py), [app/documents/telegram_link.py](../app/documents/telegram_link.py) |
| Восстановление через Telegram | [Сценарий и выпуск](telegram-password-recovery.md) | [app/web/password_recovery.py](../app/web/password_recovery.py), [app/documents/password_recovery.py](../app/documents/password_recovery.py) |
| Обязательная смена временного пароля | [Порядок выпуска и маркировка пользователей](password-change.md) | [app/web/password_policy.py](../app/web/password_policy.py), зависимости доступа в [app/portal.py](../app/portal.py) |
| Единый предел по выбранным складам | [Контракт, миграции и выпуск](warehouse-access.md) | `app/web/repository.py::Scope`, `administration.py`, `permissions.py`, `app/documents/policy.py` |
| Права пользователей dashboard | [API и доступ](CODEMAPS/api-access.md) | [app/web/administration.py](../app/web/administration.py) |
| Сотрудники iiko, создание и сверка | [API и доступ](CODEMAPS/api-access.md) | [app/web/employees.py](../app/web/employees.py) |
| Депозиты и права заведений | [API и доступ](CODEMAPS/api-access.md) | [app/web/deposits.py](../app/web/deposits.py) |
| Помощник и история диалогов | [API и доступ](CODEMAPS/api-access.md) | [app/web/assistant.py](../app/web/assistant.py) |
| Продажи, обзор, сегодняшние данные | [Аналитика](CODEMAPS/analytics.md) | [app/web/live_sales.py](../app/web/live_sales.py); складская история — [app/web/warehouse_sales.py](../app/web/warehouse_sales.py) |
| Показатели и их фильтры | [Аналитика](CODEMAPS/analytics.md) | [app/web/indicators.py](../app/web/indicators.py) |
| Закупочная цена и недельное влияние | [Аналитика](CODEMAPS/analytics.md) | [app/web/purchase_impact.py](../app/web/purchase_impact.py) |
| Аналитические списки документов/остатков/событий | [Аналитика](CODEMAPS/analytics.md) | [app/web/repository.py](../app/web/repository.py) |
| Приход в iiko, реализация и счёт PDF | [Контракт и запуск](commercial-invoices.md) | `app/commercial_invoices/`, `app/web/commercial_invoices.py`; отдельные права, миграция documents/0008 |
| Создать внешнего покупателя/поставщика | [Контракт создания](commercial-invoices.md#создание-внешнего-контрагента) | `counterparties.py`, `counterparty_dispatch.py`, `counterparty_transport.py` в `app/commercial_invoices/`; миграция documents/0009, отдельное право и флаг, неизвестный результат проверяется только GET |
| Нативные накладные, списания, согласование | [Документы](CODEMAPS/documents.md) | [app/documents/workflow.py](../app/documents/workflow.py) |
| Складские права и Telegram документов | [Документы](CODEMAPS/documents.md) | [app/documents/policy.py](../app/documents/policy.py) |
| Очередь документов и сверка `unknown` | [Документы](CODEMAPS/documents.md) | [app/documents/dispatch.py](../app/documents/dispatch.py) |
| Приватный API iiko и загрузчики | [Сбор и данные](CODEMAPS/sync-data.md) | [app/main.py](../app/main.py) |
| Архив RAW iiko в S3, backfill и восстановление | [Архив снимков](snapshot-archive.md) | `app/snapshot_archive.py`, `tools/archive_snapshots.py`; UUID/FK и бизнес-таблицы остаются в PostgreSQL |
| История OLAP и PostgreSQL | [Сбор и данные](CODEMAPS/sync-data.md) | [app/sync_sales_history.py](../app/sync_sales_history.py) |
| Плановые задания и ручной refresh | [Сбор и данные](CODEMAPS/sync-data.md) | [app/scheduler.py](../app/scheduler.py) |
| Миграции, тесты, CI | [Сбор и данные](CODEMAPS/sync-data.md) | [.github/workflows/ci.yml](../.github/workflows/ci.yml) |
| Runtime, выпуск, эксплуатационные проверки | [Операции](CODEMAPS/operations.md) | [app/serve.py](../app/serve.py) |

## Три процесса, которые нельзя смешивать

1. Публичный HTTP-портал: [main.py](../main.py) экспортирует
   [app/portal.py](../app/portal.py); [app/serve.py](../app/serve.py)
   запускает его на `PORT`.
2. Планировщик: [app/scheduler.py](../app/scheduler.py) при включённой
   синхронизации поднимает локальный приватный сборщик [app/main.py](../app/main.py)
   и выполняет задания по слотам.
3. Нативные документы: [app/documents/worker.py](../app/documents/worker.py)
   обрабатывает отдельную очередь iiko и Telegram; [app/documents/runtime.py](../app/documents/runtime.py)
   может контролировать процесс при включённых флагах.

Наличие этих процессов в коде не доказывает их фактический запуск на сервере.
После планового и ручного задания `inventory` scheduler вызывает
`app/documents/catalog_sync.py::refresh_after_inventory`: отдельные `DocumentSettings`
определяют БД и источник iiko документов. Обновляются нативный и включённый коммерческий
каталоги (GET), без обработки очередей и Telegram; общий lock document worker защищает
от пересечения со старым процессом. Занятый lock или ошибка каталога завершают задание
ошибкой с безопасным кодом и допускают штатный retry. Отключённые документы пропускаются.
`/api/health` проверяет только HTTP-процесс. Для внешних интеграций и очереди
нужны отдельные проверки из тематических карт.

## Выбор заведений и рабочих складов

[Синхронизация и склады](operation-settings.md) — настройки администратора, API/БД, границы истории и прямого OLAP, порядок выпуска.

### Поддержка и диагностика · 16.10.2026

`app/support/{models,store,capture,routes}.py`, primary `app/portal.py`, central
`app/saas_admin/server.py`; [контракт и включение](support-stage-one.md).
Отдельная PostgreSQL-схема support, session_user RLS для отдельной primary роли,
проверенные tenant/owner сессии в gateway. Кнопка обращения независима от бизнес-модулей;
свои обращения, общая очередь владельца, аудит статусов, ограниченная диагностика
без URL/headers/body/текста исключений. Миграция `20261016170000_support_stage_one.sql`.
Фоновые ошибки — `app/support_background.py` и центральный `app/saas_admin/support_monitor.py`;
границы и фактический выпуск проверять по release записи.
