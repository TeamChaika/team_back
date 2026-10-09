# Карта backend Chaika Team

Основной срез кода: **2026-10-03**, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`.
SaaS-маршруты и runtime уточнены **2026-10-09**; полный tenant dashboard реализован
в рабочей ветке, production-пилот ожидает настройки и проверки.
Карта помогает выбрать нужный модуль; она не подтверждает текущую конфигурацию production.

| Задача | Тематическая карта | Первый файл |
| --- | --- | --- |
| Изоляция полного tenant runtime | [Контракт запуска и проверки](tenant-runtime.md) | `app/tenancy/{config,sql,connection,io,locks}.py`, `app/core/config.py`, `app/web/repository.py`; UUID-схемы, restricted DB role, неизменяемый process runtime, свой collector и файлы; локально, не доказательство полного SaaS |
| Полный portal: private gateway / запуск / durable readiness | [Контракт](saas-admin.md) и [запуск](tenant-runtime.md) | `app/saas_admin/{runtime_registry,feature_readiness,runtime_visibility,full_portal_proxy,provisioning,runtime_operator,runtime_acceptance,runtime_fleet,fleet_discovery,edge_peer,provisioning_acceptance,deployment}.py`, `app/tenancy/bootstrap.py`; явное включение, company-bound UDS, restricted verifier, центральный CompanyAccounts, сохранённые проверки, очередь work/work-once и [operator CLI](../ops/tenant-runtime/README.md); default остаётся limited, локальные тесты не подтверждают полную готовность |
| Клиентский dashboard: свои Overview/Sales из Chain | [Контракт первого среза](tenant-dashboard.md) | `app/saas_admin/tenant_dashboard.py`, `dashboard_service.py`, `dashboard_transport.py`, `dashboard_reports.py`; свой membership/module gate, кэш 300с в процессе, чистые функции `app/web/overview.py`; без chaika SQL/глобальных credentials |
| Отдельный кабинет владельца SaaS, local/production реестр компаний | [Контракт и запуск](saas-admin.md) | `app/saas_admin/server.py`, `config.py`, `postgres_backup.py`, `__main__.py`, `postgres_repository.py`, `supabase_auth.py`, `connections.py`, `connection_check.py`; существующий Supabase PostgreSQL/Auth, приватная схема restcontrol, локальный Fernet key, точный HTTPS origin, Secure cookies, приватный backup/restore, loopback или Unix socket; без импортов app.portal |
| Настройки продавца, Telegram и ИИ компании | [Контракт](saas-admin.md#настройки-модулей-компании) | `app/saas_admin/company_module_settings.py`; owner-only GET/PATCH, encrypted центральное хранение, write-only ключи, версия компании и повторная подготовка runtime |
| Выпуск SaaS на постоянный сервер, TLS, резервные копии | [Изолированное развёртывание](../ops/saas-admin/README.md) | `requirements-saas-admin.txt`, `ops/saas-admin/restcontrol-saas.service`, `rc.caddy`, `restcontrol-backup.{service,timer}`; отдельный процесс на VPS 5.42.103.76 и PostgreSQL restcontrol; rc same-origin, tenant API с точным credentials CORS |
| Локальный администратор компании, временный пароль, изоляция tenant | [Контракт и запуск](saas-admin.md#доступ-администратора-компании-схема-3) | `app/saas_admin/pg_tenant_access.py`, `pg_auth.py`, `tenant_routes.py`; Supabase memberships, отдельные BFF сессии, общий React App на static App 254029 без бренда Чайки; Overview/Sales из своей Chain, остальные модули не готовы |
| Вход, сессия, рестораны, разделы | [API и доступ](CODEMAPS/api-access.md) | [app/portal.py](../app/portal.py) |
| Мой профиль, свой пароль, подключить Telegram | [API и доступ](CODEMAPS/api-access.md) | [app/web/profile.py](../app/web/profile.py), [app/documents/telegram_link.py](../app/documents/telegram_link.py) |
| Tenant восстановление через свой Telegram | [Контракт](tenant-identity.md) | `app/web/password_recovery.py`, `app/saas_admin/company_accounts.py`, private verifier `app/tenancy/bootstrap.py`; свой бот+worker, одноразовое подтверждение привязки, центральный сброс только exclusive account своей компании, без Auth admin key в child |
| Восстановление через Telegram | [Сценарий и выпуск](telegram-password-recovery.md) | [app/web/password_recovery.py](../app/web/password_recovery.py), [app/documents/password_recovery.py](../app/documents/password_recovery.py) |
| Обязательная смена временного пароля | [Порядок выпуска и маркировка пользователей](password-change.md) | [app/web/password_policy.py](../app/web/password_policy.py), зависимости доступа в [app/portal.py](../app/portal.py) |
| Единый предел по выбранным складам | [Контракт, миграции и выпуск](warehouse-access.md) | `app/web/repository.py::Scope`, `administration.py`, `permissions.py`, `app/documents/policy.py` |
| Права пользователей dashboard | [API и доступ](CODEMAPS/api-access.md) | [app/web/administration.py](../app/web/administration.py) |
| Сотрудники iiko, создание и сверка | [API и доступ](CODEMAPS/api-access.md) | [app/web/employees.py](../app/web/employees.py) |
| Tenant депозиты, свои терминалы, гостевая оплата | [Контракт](../app/tenant_payments/README.md), [провайдер](../app/tenant_payments/PROVIDER.md) | `app/tenant_payments/`, `app/tenancy/payment_bootstrap.py`; отдельная payment-role DSN, версии терминалов, авторизованная проверка ключа/merchant, sandbox/live fixed origins, provider QR currency evidence, capability-гость, unknown и сверка; локальные проверки, live origin/currency не подтверждены |
| Первая контрольная оплата владельца | [Операторская приёмка](payment-acceptance.md) | `app/tenant_payments/acceptance.py`, `acceptance_cli.py`; existing private owner session, пять setup checks, активная подписка, immutable intent до POST и safe reconcile; без HTTP обхода обычной готовности |
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
| История OLAP и PostgreSQL | [Сбор и данные](CODEMAPS/sync-data.md) | [app/sync_sales_history.py](../app/sync_sales_history.py) |
| Плановые задания и ручной refresh | [Сбор и данные](CODEMAPS/sync-data.md) | [app/scheduler.py](../app/scheduler.py) |
| Учётные записи сотрудников полного tenant portal | [Identity contract](tenant-identity.md) | `app/saas_admin/company_accounts.py`, `app/tenancy/bootstrap.py`, `app/web/{administration,profile}.py`; central-only Auth, durable request marker, отдельная identity-role, employee membership и opaque self-password; локальные PostgreSQL/HTTP проверки |
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
`/api/health` проверяет только HTTP-процесс. Для внешних интеграций и очереди
нужны отдельные проверки из тематических карт.

## Опубликованный ограниченный срез и новый полный runtime

`iiko.tdpay.ru` после публикации возвращается на общий static App Timeweb `254029`;
`api.iiko.tdpay.ru` обслуживается SaaS процессом на VPS `5.42.103.76`. Панель
`rc.chaika.team`, PostgreSQL `restcontrol` и Supabase Auth сохраняются. Точные primary
dashboard/technical host заданы через `VITE_PRIMARY_ORIGINS`. Tenant API проверяет
реестр, точный frontend Origin и собственный slug; credentials CORS, host-only
Secure SameSite=Strict cookie, CSRF. Нет wildcard или fallback к Чайке.

Прежний ограниченный runtime включает `app/web/{__init__,overview,coverage}.py` только как чистые helpers
и `defusedxml==0.7.1`; SQL-путь overview, `app.portal`, scheduler и documents worker
не подключаются. Overview/Sales читают собственную Chain, кэш процесса 300 секунд.
Наличие кода не доказывает deployment, DNS/TLS или реальную браузерную проверку.

Новый полный runtime запускает существующий `app.portal` через
`app.tenancy.bootstrap` и приватный gateway. Автоматическая подготовка:
`runtime_fleet.py` → `fleet_discovery.py` → `runtime_operator.py`; Linux-запуск
нормализует старые подписки через `Subscription` при выборе timezone (без изменения
записи/версии), изолирует UID/GID компаний через `runtime_process_identity.py` (root-owned central
config, отдельные Unix accounts, central-group sockets, fail-closed policy); центральный
вход и supervised fleet задаются в `ops/tenant-runtime/README.md`. Значение
`full_dashboard_available` сохраняет историю ранее принятого кабинета при
изменении конфигурации. `full_dashboard_ready` означает все девять проверок;
`working_dashboard_available` требует семь проверок текущей версии (без общего
modules/payments), а `feature_readiness` открывает чтение/запись отдельных функций
только по собственным HTTP/worker/provider evidence. Gateway и private verifier
проверяют эту карту; `/me` только сужает прежние ACL. Finance не входит в каталог
функций tenant и не объявляется готовым. Backup/restore сбрасывает готовности и старые
socket bindings: процессы и подключения проверяются заново.

Глобальный SaaS owner: `app/saas_admin/platform_sso.py`, `platform_sso_routes.py` →
`app/tenancy/actor.py`, `app/web/auth.py::tenant_actor_from_request`,
`Repository.actor_scope`; миграция `20261008185718_restcontrol_platform_sso.sql`.
Короткий one-use PKCE grant → company handle с FK к central session; без локального
профиля владельца. Portal требует явного `saas_auth_repository` и pinned runtime.

Первоначальная настройка полного runtime: `runtime_registry.resolve_setup` проверяет
пять сохранённых предварительных этапов и собственный private health; отдельный
`setup_available` не означает готовность. `full_portal_proxy.setup_route` открывает
только platform owner точные настройки своих терминалов и профиль до приёмки;
сотрудники и business-create закрыты. См. [ограничения настройки](tenant-runtime.md).

- `app/saas_admin/runtime_module_settings.py`: fleet и operator получают собственные seller/bot/AI из зашифрованной центральной записи с проверкой `expected_version`. Пустая центральная настройка удаляет прежнее значение операторского файла; собственные секреты не берутся из ручных файлов как fallback.
