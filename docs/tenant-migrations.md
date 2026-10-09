# Чистые схемы компании

`app.tenancy.migrations.provision_tenant(connection, runtime)` — отдельная операция
подготовки, которую выполняет доверенный оператор. Она не вызывается из HTTP,
не читает DSN из окружения и не подключается к БД самостоятельно. Production
Чайки, её миграции и данные этим этапом не изменялись.

## Контракт запуска

Передать простаивающее соединение оператора и проверенный `TenantRuntime` в режиме
`tenant`. Схемы `c_<UUID без дефисов>_{analytics,documents,payments}` и роль
`c_<UUID без дефисов>_runtime` выводятся только из UUID компании. Домен и slug
не используются как SQL identifiers. Шаблоны исполняются через `psycopg.sql`;
подстановки SQL через `.replace()` во время исполнения нет.

Оператор должен иметь CREATE DATABASE-schema / CREATEROLE и права владельца своих
схем. Это отдельный доверенный канал, не DSN веб-сервера. Runtime-роль не владеет
схемами/таблицами, не состоит в других ролях и не имеет SUPERUSER, CREATEDB,
CREATEROLE, REPLICATION, BYPASSRLS или CREATE в прикладных схемах. Роль создаётся
с LOGIN, но **без пароля**; установка собственного секрета через защищённый
операторский механизм — отдельный обязательный шаг перед внешним подключением.
Runtime обязан пройти `validate_database_runtime()` своим реальным соединением.

Минимальный вызов в доверенном управляющем коде:

```python
with psycopg.connect(operator_dsn, autocommit=True) as connection:
    applied = provision_tenant(connection, runtime)
```

`applied` содержит только применённые имена миграций. Повтор возвращает `()`.
Это не результат готовности компании и не включает создание Auth-аккаунтов,
подключение iiko, DNS/TLS, синхронизацию, запуск workers или проверку модулей.

## Порядок и происхождение

Файлы заведены через `supabase migration new` CLI 2.34.3 в отдельном локальном
каталоге авторинга, затем помещены в `migrations/tenant`. Их нельзя применять
через обычный `supabase db push`: они содержат проверяемые identifier-шаблоны.
Порядок явно задан в `manifest.json`, история обычных миграций Чайки не менялась.

1. **Analytics baseline**: schema-only состояние всех 29 аналитических миграций
   `supabase/migrations/20260910171101…20261007190000`, за исключением отдельного
   реестра `restcontrol`. Восстановлено с нуля в одноразовом PostgreSQL, без
   production dump/data, затем экспортированы DDL, ограничения, индексы,
   operation-specific ACL и RLS. Включены складские права, подготовленные
   закупочные цены/влияние, помощник, планировщик, история/справочники/OLAP.
   Результат: 60 таблиц, 5 представлений, без учёта журнала миграций.
2. **Documents base**: 14 необходимых таблиц бывшего Django-приложения: пользователи,
   отделы, склады, накладные/строки, списания/строки/причины, portal user links,
   grants, operations, notifications и audit events. DDL сверены с доступными
   Django-моделями `iiko.chaika.team/app/src/dashboard/{authentication,departments,
   stores,waybills,writeoffs}/models.py` и текущими потребителями
   `app/documents/{policy,administration,workflow,operations,reads}.py`.
   Структурный dump из `tests/fixtures/documents_schema.sql` использован только
   как источник подтверждённых столбцов/индексов/FK. **Синтетические** `portal_access`
   и `portal_warehouse_scope_test` из хвоста fixture исключены. Новый
   `documents.portal_access` — security-invoker view собственных
   `analytics.web_users`, а warehouse view использует настоящие tenant ACL.
   Runtime не запускает Django: таблицы Django auth groups/permissions, sessions,
   migration history, старые регистрации и M2M stores не требуются и не создаются.
3. **Native documents**: 0001–0007 в исходном порядке: jobs/catalog/dispatch/bot
   updates, Telegram link, recovery, расхождения при приёмке, стоимости/техкарты,
   Telegram cleanup ledger. Обратное заполнение старых сообщений исключено:
   новая компания не имеет старых сообщений.
4. **Commercial documents**: 0008–0010: приход/реализация, grants/revisions/events/
   dispatch, контрагенты и права на создание, чтение своей warehouse ACL.
   После расширений documents содержит 35 таблиц, 1 view и 14 последовательностей.
5. **Platform document actors**: дополнительная миграция
   `20261008190049_tenant_platform_document_actors.sql` добавляет JSONB-снимки
   авторов и append-only `native_actor_audit`. Старые строки и локальные FK
   сохраняются; фиктивные пользователи не создаются.
6. **Payments**: `20261008230000_tenant_payments.sql` создаёт 8 пустых таблиц
   (venues, terminals, terminal_versions, deposit_grants, deposits, attempts,
   audit, webhook_receipts), плюс журнал. Отдельная роль
   `c_<companyhex>_payments_runtime` не получает доступ к analytics/documents;
   основная runtime-роль не читает платёжные таблицы. Данные и ключи не копируются.
   Создание credentials и настройка провайдера остаются отдельным шагом.
7. **Company identity boundary**: `20261008200833_tenant_company_identity_boundary.sql`;
   отдельная роль с доступом только к трём функциям создания сотрудника,
   проверки локального admin и завершения смены пароля. Удалены только FK
   глобальных авторов аналитики; subject FK сохранены. [Контракт](tenant-identity.md).

Исходные SQL-файлы и их SHA-256 перечислены в `migrations/tenant/provenance.json`.
Новые версии приложения требуют дополнительных tenant-миграций: уже применённые
baseline-файлы редактировать нельзя.

## Auth и важное отличие от Чайки

Исходное `portal_identities` читало все `auth.users` с правами владельца view.
Такое представление клиенту не переносится. Вместо него есть собственная
`portal_identity_metadata(id,email,provision_id)` и security-invoker view.
Runtime может читать cache, но не записывать его. Доверенный onboarding/управление
учётными записями должен заполнять cache только после проверки Supabase Auth и
membership выбранной компании. Для записи добавлен приватный SQL API
с отдельной identity-ролью; [контракт и ограничения](tenant-identity.md).
FK из `web_users.id` на общую `auth.users` не переносится; проверка глобального
Auth UUID и удаления/отзыва доступа остаётся обязанностью управляющего слоя.
Это позволяет воспроизводить baseline в пустой PostgreSQL без поддельной Auth.

Снимки глобальных авторов добавлены отдельной миграцией поверх baseline.
В `waybills`/`writeoffs` это `created_actor` и `processed_actor`, в
`commercial_invoices` — `created_actor`; в `portal_documents_event`,
`portal_documents_operation`, `commercial_invoice_operations`,
`commercial_invoice_events`, `commercial_counterparty_operations` — `actor`.
Все снимки JSONB содержат `company_id`, `auth_user_id`, `kind`, `display_name`,
`membership_id` (nullable). SQL проверяет типы, UUID своей компании, ненулевой
Auth UUID, непустое имя и kind `platform_owner`/`company_member`. У platform
owner membership должен быть null. Обязательный bigint FK теперь допускает
null исключительно вместе с валидным `platform_owner`; системные события и
ещё не обработанные документы по-прежнему допускают отсутствие автора.
Старые локальные строки не требуют снимка и сохраняют FK. Даже при наличии
локального FK нельзя записать некорректный снимок или UUID другой компании.

`native_actor_audit` сохраняет UUID компании/автора, вид и имя автора, действие,
вид/ID объекта, JSON-данные и время. Runtime получает только INSERT/SELECT;
UPDATE/DELETE/TRUNCATE запрещены, RLS/FORCE RLS применяет общий runner.
Эти ограничения проверяют согласованность хранения; действительность сессии,
роль и отзыв доступа проверяет доверенный auth-слой приложения.

## Изоляция и повтор

Один transaction-scoped advisory lock выводится из UUID + `migrations`.
Создание роли/схем, вся последовательность SQL, журналы и grants находятся в
**одной транзакции**. Ошибка откатывает и DDL, и роль, и запись в журнал.
Одновременные запросы одной компании сериализуются; разные компании используют
разные ключи. `lock_timeout=5s` ограничивает ожидание, после таймаута безопасен повтор.

В каждой схеме свой `_tenant_migrations` с company UUID, именем, SHA-256 и временем.
Журнал закрыт от runtime. Проверяются чужой UUID, checksum, неизвестные миграции
и нарушенный порядок. Существующая схема без журнала или с другим владельцем
не усыновляется. Вновь созданные таблицы закрываются от PUBLIC/anon/authenticated/
service_role; схемы не добавляются в Supabase Data API. Все прикладные таблицы
имеют RLS/FORCE RLS; существующие аналитические append-only grants сохранены,
документам выданы только необходимые операции. Журнал имеет RLS без FORCE,
чтобы доверенный владелец мог обслуживать его и без superuser.

Используются встроенные PostgreSQL 15+ `sha256`, `gen_random_uuid`, JSONB,
security-invoker views и advisory locks. Дополнительные расширения, в том числе
pgcrypto, не нужны: baseline прошёл на `TEMPLATE template0`. Existing shared
extensions не изменяются. Проверка схемы не заменяет проверку прав/настроек
конкретного managed Supabase при разрешённом внедрении.

## Проверки

```sh
RESTCONTROL_MIGRATIONS_TEST_DSN='host=127.0.0.1 port=55483 dbname=postgres user=tenant_test_admin' \
  python -m pytest tests/test_tenant_migrations.py tests/test_tenant_migrations_postgres.py -q
```

Только явный loopback DSN одноразового кластера; тесты создают отдельную БД из
`template0`, удаляют её и тестовые роли в конце. Production DSN не берётся из
настроек приложения. Без переменной интеграционные тесты пропускаются.

Подтверждено локально 08.10.2026: 7 tests passed. Два пустых клиента, совпадающие
source/user/store/document/request IDs; чтение/изменение чужой схемы, SET ROLE,
DDL, подмена журнала, запись identity cache и удаление append-only истории
запрещены. Проверены повтор без потери данных, настоящий rollback ошибки SQL,
отказ checksum mismatch и конкурентный запуск (один apply, один no-op). Отдельно проверен владелец
миграций без SUPERUSER/BYPASSRLS: повтор и оба ACL-представления работают.
Это доказательство SQL-изоляции; работа полного HTTP/dashboard, iiko, платежей
и реальных callbacks отдельной компании требует следующих этапов.

Проверка actor delta: `tests/test_tenant_actor_schema.py` — создание всех видов
документных записей глобальным автором без локального пользователя, отказ
некорректным/чужим снимкам, append-only audit и применение поверх существующей
локальной накладной без изменения её автора.
