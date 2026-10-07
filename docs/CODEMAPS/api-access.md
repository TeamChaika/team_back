# API, доступ и пользовательские функции

Срез: 2026-10-03, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`. Здесь указаны связи по коду, а не состояние production.

Публичный процесс: [main.py](../../main.py) экспортирует [app/portal.py](../../app/portal.py); [app/serve.py](../../app/serve.py) запускает приложение на `PORT`. `/api/health` подтверждает только жизнь HTTP-процесса.

| Задача | Файлы и путь вызова |
| --- | --- |
| Вход/сессия | [app/portal.py](../../app/portal.py) `/api/auth/*` → [app/web/auth.py](../../app/web/auth.py) → Supabase Auth; профиль проверяется через [app/web/repository.py](../../app/web/repository.py) |
| Забытый пароль | `/api/auth/recovery/*` → [password_recovery.py](../../app/web/password_recovery.py); только ранее привязанный Telegram, одноразовый хешированный токен, admin Auth password update с отзывом сессий. [Контракт и выпуск](../telegram-password-recovery.md) |
| Временный пароль | `chaika.web_users.password_change_required` читается при каждом запросе; `access` и `portal_access` вызывают [password_policy.py](../../app/web/password_policy.py). До смены разрешены только login/refresh/logout, минимальный `/api/me` и `POST /api/profile/password`. После подтверждения Auth вызывается `Repository.complete_password_change`. Новые аккаунты получают флаг по умолчанию; [разовая маркировка существующих](../password-change.md) выполняется отдельным CLI, без доступа runtime к Auth-хэшам |
| Мой профиль | `/api/profile/*` → [app/web/profile.py](../../app/web/profile.py): смена пароля через повторный вход того же пользователя и Supabase `PUT /user`; Telegram → [telegram_link.py](../../app/documents/telegram_link.py). Доступ всем активным аккаунтам через `portal_access`, без требования аналитических разделов. Origin и лимиты проверяются для записи; идентификатор только из сессии |
| Выбор ресторанов | `access` в [app/portal.py](../../app/portal.py) → `Repository.scope` → `chaika.web_users`, `web_department_access`, `corporate_nodes`, `stores`, `rms_bindings`; `Scope.ids` ограничивает доступные подразделения |
| Разделы | [app/web/permissions.py](../../app/web/permissions.py) задаёт `sections_for`/`require_section`/`section_for_path`; права проверяются на API, включая режим «только документы» |
| Управление dashboard | `/api/management/*` → [app/web/administration.py](../../app/web/administration.py); `require_admin` и аудит прав портала; это не назначение складских прав документов |
| Сотрудники iiko | `/api/employees*` → [app/web/employees.py](../../app/web/employees.py) `EmployeeEditor` → [employee_write.py](../../app/integrations/iiko/employee_write.py); при спорном ответе используется сохранённый запрос и `reconcile` |
| Депозиты | `/api/deposits/*` → [app/web/deposits.py](../../app/web/deposits.py) → отдельный pay API с пользовательским Bearer; доступ к заведениям проверяет upstream. Dashboard не хранит копию депозитов |
| Помощник | `/api/assistant/messages` → [app/web/assistant_store.py](../../app/web/assistant_store.py) резервирует лимит/диалог → [app/web/assistant.py](../../app/web/assistant.py) вызывает ограниченные `Scope` инструменты и модель → сохраняет ответ/источники |

`Repository.connection` открывает аналитические запросы `READ ONLY`. Запись идёт через отдельные модули: управление профилями, редактирование сотрудников, диалоги помощника, нативные документы и upstream депозитов. Права `portal_documents_grant` не выводятся из `Scope.store_ids`; их карта — [documents.md](documents.md).

Схемы: [portal_and_olap.sql](../../supabase/migrations/20260911013009_chaika_portal_and_olap.sql) создаёт профиль/назначения; [portal_administration.sql](../../supabase/migrations/20260928143000_portal_administration.sql) разрешает администрирование; [deposit_portal_role.sql](../../supabase/migrations/20260928094000_deposit_portal_role.sql) добавляет роль депозитов; [purchase_assistant.sql](../../supabase/migrations/20260914194141_purchase_assistant.sql) хранит диалоги; [employee_editing.sql](../../supabase/migrations/20260915233000_employee_editing.sql) хранит изменения сотрудников.

Тесты по границе: [test_portal.py](../../tests/test_portal.py), [test_document_only_scope.py](../../tests/test_document_only_scope.py), [test_web_auth_resilience.py](../../tests/test_web_auth_resilience.py), [test_deposits.py](../../tests/test_deposits.py), [test_employee_editing.py](../../tests/test_employee_editing.py).

## Выбранные склады

[Единый предел по складам](../warehouse-access.md): `warehouse_scope_mode` и
`web_warehouse_access` сужают прежние права; `Scope.unrestricted` не допускается
для selected. Account API сохраняет предел при отсутствии новых полей в старом
клиенте. `/api/me` возвращает warehouse_scope и warehouse_capabilities. Разделы
без проверенной складской привязки закрывает `require_warehouse_section`,
включая прямые запросы к деталям, экспорту и командам.
