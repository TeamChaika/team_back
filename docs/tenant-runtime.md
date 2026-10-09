# Изолированный runtime компании

Код `app/tenancy` задаёт неизменяемую компанию **при запуске процесса**.
Входящий Host, Origin, cookie или параметр HTTP не переключает схему базы данных.
Одна компания — отдельный процесс portal/collector/worker из общей версии приложения.

Без переменных `RESTCONTROL_TENANT_*` и с отсутствующим либо равным `legacy`
`RESTCONTROL_RUNTIME_MODE` сохраняются исторические настройки и схемы Чайки.
Наличие tenant-переменных без явного режима `tenant` завершает запуск ошибкой.

## Обязательная конфигурация

В режиме `RESTCONTROL_RUNTIME_MODE=tenant` обязательны:

| Переменная с префиксом `RESTCONTROL_TENANT_` | Смысл |
| --- | --- |
| `COMPANY_ID` | Ненулевой UUID компании |
| `TIMEZONE` | Явная IANA timezone |
| `FRONTEND_ORIGIN`, `API_ORIGIN` | Разные точные HTTPS origins без пути/wildcard; домены Чайки запрещены |
| `RUNTIME_DIRECTORY` | Абсолютный путь, заканчивающийся на `c_<полный UUID без дефисов>` |
| `CONFIGURATION_VERSION` | Положительная версия конфигурации |
| `DATABASE_ROLE` | `c_<полный UUID без дефисов>_runtime` |
| `DATABASE_URL` | DSN выделенной ограниченной роли; проверяется фактический login PostgreSQL |
| `IIKO_BASE_URL`, `IIKO_LOGIN`, `IIKO_PASSWORD` | Собственное подключение Chain, без fallback к `CHAIKA_*` |
| `WEB_SUPABASE_URL`, `WEB_ANON_KEY` | Явная конфигурация общего сервиса Auth |

Схемы вычисляются только из UUID: `c_<hex>_analytics`, `c_<hex>_documents`,
`c_<hex>_payments`. Настройки backend читают поля из `RESTCONTROL_TENANT_*`,
web из `RESTCONTROL_TENANT_WEB_*`, помощник из `RESTCONTROL_TENANT_AI_*`.
В этих режимах `.env`, `.env.assistant`, глобальные file secrets и `CHAIKA_*`
не используются. Опциональные функции без собственных credentials остаются
ненастроенными. Старые URL pay/documents не наследуются.

Приватный collector использует `<runtime directory>/collector.sock`; при явном
`COLLECTOR_PORT` HTTP обращается к своему loopback-порту. Tenant scheduler всегда
использует отдельно запущенный collector и проверяет company/version через private
health; отсутствие отдельного collector останавливает worker без запуска второго
сборщика, без зависимости от `RESTCONTROL_TENANT_EXTERNAL_COLLECTOR`.
Tenant portal не запускает встроенный scheduler: fleet запускает отдельный
`run-scheduler` только после успешного `initial_sync`. В legacy portal встроенный
scheduler сохраняется. Перед обновлением миграций fleet останавливает только свои
процессы компании; запуск возобновляется после подтверждения текущего fingerprint
manifest. Сохранённый database_roles.ok сам по себе запуск не разрешает. Распределение уникальных
портов контролирует процесс provisioning. Временные файлы и сохранённые расшифровки
попадают в директорию компании. Advisory locks хешируют компанию, назначение и ресурс.
Общий лимит лицензии одного физического iiko сервера требует дополнительного
межпроцессного ограничения в слое синхронизации.

Первоначальная загрузка остатков передаёт обязательный `--timestamp`: текущее
учётное время компании без UTC offset, как плановая синхронизация. Выбранный срез
сохраняется в `initial_sync.evidence.balance_timestamp`; период истории продаж и
документов остаётся отдельным. При ошибке сохраняются только имя задания и exit code,
при timeout — имя задания; следующие задания не запускаются, вывод и секреты не пишутся.

План первоначальной истории версии 2 (`app/saas_admin/initial_sync_plan.py`) явно
запрашивает `writeoffs`, `invoices`, `outgoing`, `transfers` у существующего
`sync_documents`. Кассовые смены собираются существующим `sync_cash_shifts --from
… --to …` последовательными интервалами до семи дней включительно по всему выбранному
периоду. Evidence сохраняет `plan_version`, `document_resources`, `cash_shift_windows`
и завершённые задания только после успеха всех процессов. Старое подтверждение без
этого покрытия повторно проверяется Provisioner; fleet останавливает только свой
scheduler компании и возвращает завершённый старый план в очередь без изменения
версии компании. Portal и collector могут продолжать проверку health. Полная,
рабочая и setup-готовность требуют совместимого подтверждения текущего плана;
ранее принятый recovery-доступ сохраняется отдельно. Существующие загрузчики
используют собственные контрольные точки истории; план не заменяет доказательства
реальных данных и не создаёт их.

## SQL и подключения

`render()` принимает только SQL-шаблон разработчика и проверенные identifiers;
значения остаются `%s`-параметрами. Для прежних составных SQL builders используются
неизменяемые `ANALYTICS_SCHEMA`, `DOCUMENTS_SCHEMA`, `PAYMENTS_SCHEMA`, сформированные
через `psycopg.sql.Identifier.as_string()`. Изменение схем выполняется в исходниках;
произвольная подмена текста SQL во время исполнения отсутствует.

`Repository` использует `configure_connection` на каждой новой pool connection;
`tenant_connect` выполняет ту же проверку для отдельных аналитических запросов.
До работы запрещаются чужой/session-switch login, privileged роли, membership,
CREATE на схемах, доступ к чужим application schemas, таблицам, колонкам и sequences.
Привилегированный оператор миграций не является ролью runtime. Изменение grants во
время работы требует перезапуска процессов; проверка не заменяет общий аудит
SECURITY DEFINER функций/расширений в используемом Supabase проекте.

Portal хранит runtime в `app.state.tenant_runtime`, открывает проверенный pool до
старта фоновых заданий и допускает Host собственного API. CORS использует точный
frontend origin. Авторизация и складские права продолжают проверяться существующими
обработчиками; создание полной platform-сессии владельца — отдельный контракт.

## Локальные доказательства и границы

`tests/test_tenant_runtime*.py` проверяют конфигурацию, SQL identifiers, namespace
файлов/locks и реальные отказы PostgreSQL. Для DB-тестов используется только явный
`RESTCONTROL_RUNTIME_TEST_DSN` одноразовой локальной базы. На двух ограниченных ролях
с одинаковым UUID пользователя проверен настоящий `Repository.open()` и
`portal_scope()`: возвращается отображаемое имя только своей компании.

Это не доказательство полной готовности SaaS: платёжные таблицы/операции, platform
actor, автоматическое добавление сотрудников в private identity registry,
все миграции/синхронизации и пользовательские сценарии требуют отдельной приёмки.
Production этими проверками не меняется.

## Explicit portal launcher

`python -m app.tenancy.bootstrap --config /private/company.json` accepts a mode-0600
JSON environment manifest containing only `RESTCONTROL_RUNTIME_MODE=tenant` and
explicit `RESTCONTROL_TENANT_*` fields. It removes inherited operator/global secrets,
validates the pinned runtime, requires a mode-0700 company directory and launches the
existing portal on `portal.sock` under a private umask. Additional required fields:
`RESTCONTROL_TENANT_VERIFIER_SECRET_FILE` (0600 capability file) and
`RESTCONTROL_TENANT_VERIFIER_SOCKET` (central private UDS). `build_portal()` injects
the restricted verifier and commercial service owner revalidation. Process supervision,
real provisioning adapters and worker launch policy are separate operator work; the
launcher does not imply module or external acceptance success.

Actual central assembly uses `app.saas_admin serve --runtime-config` and the same
trusted private configuration as `runtime_operator serve-verifier`. See
[operator lifecycle](../ops/tenant-runtime/README.md) for explicit worker/scheduler
commands, scoped settings, own-session read probes and concrete readiness blockers.
Neither factory creation nor a successful probe invents external provider acceptance.

### Первоначальная настройка до общей приёмки

`setup_available` в публичном context и authenticated workspace означает только:
для текущей версии сохранены успешные `migrations`, `database_roles`, `identity`,
`connections`, `initial_sync`, а собственный private portal отвечает health с тем
же UUID и версией. Это не `full_dashboard_ready` и не принятый runtime.
Gateway дополнительно требует действующую company-bound сессию platform owner,
свой Origin, CSRF для POST и актуальные feature entitlements. Сотрудникам режим
не открыт. Разрешены только GET me, management venues/accounts, payment-settings,
profile telegram; POST password/logout, UUID venue/terminal settings и точный
payment-settings terminal validate. Создание пользователей, документов, депозитов,
гостевой платеж и остальные бизнес-маршруты этим режимом не разрешаются.

Владелец может сохранить собственный терминал и отдельно запустить его проверку.
Read-only provider validation не заменяет доказательство settlement/currency:
payments acceptance остаётся pending до реального допустимого доказательства.
Этот режим сам по себе не организует приёмочный платёж и не включает полный кабинет.
Реквизиты продавца, собственный Telegram bot и assistant provider пока подаются
через приватные operator settings; пользовательского self-service API для этих
настроек здесь нет. После настройки требуется повторный запуск durable приёмки.
