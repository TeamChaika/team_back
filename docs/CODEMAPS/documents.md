# Нативные накладные и списания

Срез: 2026-10-03, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`. Это домен `chaika_iiko_documents`, отдельный от аналитических `chaika.*`. Подробный порядок переключения и сверки — [../documents.md](../documents.md).

| Задача | Файлы и путь вызова |
| --- | --- |
| Выбор реализации | [app/portal.py](../../app/portal.py) выбирает `DocumentService` при `native_enabled` или `DocumentsClient` прежнего сервиса; маршруты `/api/documents/*` в [app/web/documents.py](../../app/web/documents.py) |
| Список/карточка/CSV | [app/web/documents.py](../../app/web/documents.py) → [app/documents/service.py](../../app/documents/service.py) → [app/documents/reads.py](../../app/documents/reads.py); право склада проверяется и в списке, и в деталях |
| Создать/изменить/копировать | `DocumentService.dispatch` → [app/documents/workflow.py](../../app/documents/workflow.py) `mutate` → [app/documents/catalog.py](../../app/documents/catalog.py), [app/documents/policy.py](../../app/documents/policy.py); `request_id` и `version` защищают от повторов/гонок |
| Согласовать/отклонить | `mutate` проверяет профиль, склад, действие, версию, состояние; `confirm` сохраняет операцию и задачу отправки в одной транзакции. Ответ сайта означает сохранение согласования, не проведение в iiko |
| Отправить | [app/documents/dispatch.py](../../app/documents/dispatch.py) → [app/documents/transport.py](../../app/documents/transport.py); `queued`, `sending`, `sent`, `unknown` различаются. Неопределённый POST не повторяется автоматически; сверка — [app/documents/reconcile.py](../../app/documents/reconcile.py) |
| Telegram | [app/documents/worker.py](../../app/documents/worker.py) → [app/documents/telegram.py](../../app/documents/telegram.py) и [app/documents/messages.py](../../app/documents/messages.py); callback согласования возвращается в `workflow.mutate` с проверкой прав и версии |
| Доступ склада | [app/documents/policy.py](../../app/documents/policy.py): `portal_access` → `portal_documents_userlink` → `authentication_user` → `portal_documents_grant`; администратор портала не получает автоматически согласование всех складов |
| Самостоятельная привязка Telegram | `/api/profile/telegram*` → [app/documents/telegram_link.py](../../app/documents/telegram_link.py): одноразовая ссылка на 10 минут, хеш токена в БД; [telegram.py](../../app/documents/telegram.py) принимает `/start link_…` только в личном чате; [worker.py](../../app/documents/worker.py) хеширует токен до сохранения входящего сообщения |
| Управление профилями | [app/documents/administration.py](../../app/documents/administration.py) связывает dashboard-профиль, складские действия и Telegram ID без расширения аналитических прав |

Нативный worker — отдельный процесс [app/documents/worker.py](../../app/documents/worker.py). [app/documents/runtime.py](../../app/documents/runtime.py) может контролировать его из процесса портала при включённых флагах; [../documents.md](../documents.md) также описывает самостоятельный запуск. Код не подтверждает текущий хост или включение production. Лидерство, очередь и heartbeat документов отдельны от cron аналитики. `/api/health` их не проверяет.

Схема: [0001_native_runtime.sql](../../migrations/documents/0001_native_runtime.sql) добавляет runtime-таблицы существующей закрытой схемы; [documents_schema.sql](../../tests/fixtures/documents_schema.sql) — тестовая копия, не production-миграция. Тесты: [test_native_documents.py](../../tests/test_native_documents.py), [test_document_transport.py](../../tests/test_document_transport.py), [test_document_only_scope.py](../../tests/test_document_only_scope.py).

Миграция [0002_telegram_link.sql](../../migrations/documents/0002_telegram_link.sql) добавляет только таблицу одноразовых токенов и явные права runtime. Регрессии привязки: [test_telegram_link.py](../../tests/test_telegram_link.py), отдельная loopback БД `telegram_profile_tests`.
