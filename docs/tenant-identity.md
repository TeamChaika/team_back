# Приватная граница учётных записей компании

Миграция `20261008200833_tenant_company_identity_boundary.sql` выполняется только
общим tenant runner. Она не создаёт Supabase Auth-аккаунтов или platform owner.
Доверенный центральный сервис сначала проверяет свою сессию, компанию, права
инициатора и настоящий Auth UUID, затем подключается отдельной ролью
`c_<UUID без дефисов>_identity_runtime`. Пароль роли устанавливает оператор
отдельно; миграция не читает credentials.

У роли есть только USAGE собственной analytics-схемы и EXECUTE трёх функций:

| Функция | Результат |
| --- | --- |
| `provision_company_identity(uuid, text email, text provision_id, text display_name)` | UUID сотрудника |
| `set_company_identity_password_required(uuid, boolean)` | void; допустим только `false` после проверенной смены собственного пароля |
| `company_identity_actor_is_admin(uuid)` | true только для активного локального portal admin |

Первая функция атомарно создаёт нейтральный `web_users`: manager, без sections,
без административных прав и складских grants, `warehouse_scope_mode=selected`,
обязательная смена пароля. Одновременно создаются настоящий профиль сотрудника
`authentication_user` с непригодным для входа локальным паролем `!`,
`portal_documents_userlink` с тем же Auth UUID и запись `portal_identity_metadata`.
Эта операция предназначена только сотрудникам; глобальному владельцу личная
строка компании не нужна и не создаётся.

Повтор с тем же UUID, email и provision_id возвращает прежний UUID, сохраняя
права, отключённое состояние и связь рабочего профиля. Другой marker/email,
занятый email либо ранее существовавший профиль без этой записи provisioning
вызывают отказ. Доступ к складам и разделам задаётся отдельно существующим
процессом администрирования.

Identity-роль не читает и не изменяет таблицы напрямую, не имеет sequence/DDL
прав, не состоит в других ролях и не получает доступ к другой компании.
Основная runtime-роль читает локальный metadata cache, но не пишет его и не
вызывает функции provisioning. Runner проверяет эти ограничения перед и после
применения миграций; расширенные права приводят к отказу.

Функции используют SECURITY DEFINER, фиксированный `search_path=pg_catalog` и
полностью квалифицированные таблицы. Владелец функций — существующий доверенный
оператор миграций. FORCE RLS сохраняется; четыре policy для этого оператора
разрешают фиксированные действия с web_users, metadata, рабочими профилями и
связями. Это осознанный вариант без дополнительной NOLOGIN writer-роли:
PostgreSQL 17 требует сохранять ADMIN-членство оператору для последующего
повторного назначения владения отдельной роли. Здесь дополнительные членства
и временное расширение CREATE/SET ROLE не требуются. Центральный сервис при этом
использует только ограниченную identity-роль, а не DSN оператора.

В этой же миграции убраны только три FK, ошибочно связывавшие глобального автора
с локальным сотрудником: `employee_changes.user_id`,
`assistant_conversations.user_id`, `sales_report_sets.reviewed_by`. FK субъектов
доступа и связи assistant turn → conversation сохранены. Assistant RLS по-прежнему
сравнивает UUID пользователя с локальным параметром
`c_<UUID без дефисов>_analytics.assistant_user`; параметр должен задаваться внутри
транзакции на проверенном соединении компании.

Проверки `tests/test_tenant_identity_schema.py` используют одноразовые локальные
БД: настоящее создание профиля/связи, безопасный повтор, отказы подмене,
изолированную роль, глобальных авторов без web_users и исполнение функций от
оператора без SUPERUSER/BYPASSRLS. Внешний Supabase Auth проверяется центральным
сервисом отдельно; эти SQL-тесты не являются проверкой production.

## Центральный Auth и существующие формы портала

`app/saas_admin/company_accounts.py::CompanyAccounts` работает только в control
plane с `PostgresRepository` и серверным Supabase Auth client. Operator передаёт
явное отображение company UUID → `IdentityTarget(runtime, identity_dsn)`; target
проверяет login, отсутствие привилегий и доступ только к трём собственным
функциям. DSN и глобальный Auth admin key не передаются дочернему portal.

`create_verifier_app(..., company_accounts=service)` разрешает операции
`account-create` и `password` исключительно capability с ролью `portal`.
`RestrictedVerifier` отправляет непрозрачный session token и CSRF. Центральный
сервис заново проверяет сессию, компанию и CSRF, а для создания — активный
локальный `is_portal_admin` либо проверенного platform owner. Worker capability
не имеет этих операций. Caller UUID из тела не принимается.

CLI-миграция `20261008200905_restcontrol_company_accounts.sql` добавляет приватный
`restcontrol.company_account_requests`. Запись фиксируется **до** Auth POST.
Повтор pending-запроса только сверяет Auth marker: новый POST не выполняется.
Если пользователь появился после потерянного ответа, тот же request_id завершает
создание membership и своей локальной metadata. Чужая уже существующая почта
отклоняется без attach/reset. Журнал хранит защищённый HMAC отпечаток входа и
зашифрованный случайный ключ проверки, а не пароль или JWT.

Новый membership имеет `role=employee`, `is_primary_admin=false` и обязательную
смену временного пароля. Tenant login и проверка opaque session допускают этот
тип membership. SQL routine создаёт нейтральный локальный профиль, затем
`Administration.save_identity_account` сохраняет выбранные в текущем UI права
через обычную проверку scope. Повтор не сбрасывает позднейшие изменения прав.

`/api/profile/password` в tenant-режиме вызывает private verifier. Центральная
проверка использует текущий пароль именно владельца opaque session. Для
сотрудника обновляется membership и снимается локальный password gate. Для
platform owner обновляются токены родительской центральной сессии, а непрозрачный
company handle меняется; локальный профиль владельца не создаётся. JWT наружу
не возвращается. Ответ содержит новый CSRF и строго scoped secure HTTP-only
`saas_tenant_session`; внешний gateway обязан передавать только эту cookie и
разрешать password route при включённом password gate. Если локальная очистка
password gate не подтвердилась, новый opaque handle сохраняется, но возвращается
503 для повторной проверки.

Запуск identity API требует явного подключения `CompanyAccounts` к verifier и
новой центральной миграции, отдельного пароля identity-role и private target
configuration. Отсутствующая интеграция отвечает 503, не использует глобальные
ключи или старую базу как fallback. Старый ограниченный central dashboard не
должен выдавать сотруднику полный обзор компании вместо складских/section ACL
полного portal.

`tests/test_company_accounts_postgres.py` проверяет центральный журнал и
настоящие SQL-роли двух компаний с синтетическим Auth provider: создание,
unknown recovery без повторного POST, чужую почту, employee login/self-password,
owner parent refresh без личного аккаунта, capability/CSRF/локальные права и
существующие HTTP формы management/profile. Supabase live и production не
используются.

Миграция `20261008202028_tenant_account_initialization.sql` хранит локальную
квитанцию первоначальной настройки. Профиль и платёжные grants могут находиться
на разных DSN, поэтому повтор после сбоя синхронизации завершает оставшийся шаг.
Revision профиля и курсор платёжного audit защищают более поздние правки другого
администратора; guarded sync блокирует grant/revoke DML на время сверки и записи.
Журнал помечается завершённым только после синхронизации либо обнаружения более
нового административного решения. Пароли в эту квитанцию не попадают.
Признак `profile_applied` записывается атомарно с первоначальным профилем: сама
по себе revision=2 не доказывает, что её сохранил данный initializer. Поэтому
конкурирующая правка администратора не разрешает восстановление старых grants.
Явные Auth 409/429 без собственного marker переводят центральный запрос в
`rejected`: исправленные данные можно отправить новым request_id. Неопределённый
transport/5xx по-прежнему остаётся pending без повторного POST.

### Telegram recovery in a tenant

The private `recovery` capability accepts only the raw one-time token and new
password. `CompanyAccounts.recover_password` selects the configured company target;
no caller-supplied Auth UUID is accepted. Migration
`20261009190000_tenant_password_recovery.sql` adds two execution-only routines for
that company's identity login. They consume the hashed proof durably, then recheck
and lock the current Telegram binding, revision and active profile through the Auth
request. The identity login has no document-table privileges.

Central recovery requires one active, exclusive membership in that exact active
company and rejects any platform identity. Existing company sessions are revoked
before Auth; ambiguous provider results consume the proof and never retry the
password operation. Success also clears the temporary-password flag, revokes any
intervening company sessions and records a recovery event. The response does not
create a session or return provider tokens. A failed local completion returns
`completed=false` after confirmed Auth success so the caller can instruct a new login.
