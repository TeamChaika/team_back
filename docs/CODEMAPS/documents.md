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
| Восстановление пароля | [password_recovery.py](../../app/documents/password_recovery.py): `/start recover` + подтверждение только в private chat; существующая активная уникальная привязка; миграция [0003](../../migrations/documents/0003_password_recovery.sql), [сценарий](../telegram-password-recovery.md) |
| Самостоятельная привязка Telegram | `/api/profile/telegram*` → [app/documents/telegram_link.py](../../app/documents/telegram_link.py): одноразовая ссылка на 10 минут, хеш токена в БД; [telegram.py](../../app/documents/telegram.py) принимает `/start link_…` только в личном чате; [worker.py](../../app/documents/worker.py) хеширует токен до сохранения входящего сообщения |
| Управление профилями | [app/documents/administration.py](../../app/documents/administration.py) связывает dashboard-профиль, складские действия и Telegram ID без расширения аналитических прав |

Нативный worker — отдельный процесс [app/documents/worker.py](../../app/documents/worker.py). [app/documents/runtime.py](../../app/documents/runtime.py) может контролировать его из процесса портала при включённых флагах; [../documents.md](../documents.md) также описывает самостоятельный запуск. Код не подтверждает текущий хост или включение production. Лидерство, очередь и heartbeat документов отдельны от cron аналитики. `/api/health` их не проверяет.

Схема: [0001_native_runtime.sql](../../migrations/documents/0001_native_runtime.sql) добавляет runtime-таблицы существующей закрытой схемы; [documents_schema.sql](../../tests/fixtures/documents_schema.sql) — тестовая копия, не production-миграция. Тесты: [test_native_documents.py](../../tests/test_native_documents.py), [test_document_transport.py](../../tests/test_document_transport.py), [test_document_only_scope.py](../../tests/test_document_only_scope.py).

Миграция [0002_telegram_link.sql](../../migrations/documents/0002_telegram_link.sql) добавляет только таблицу одноразовых токенов и явные права runtime. Регрессии привязки: [test_telegram_link.py](../../tests/test_telegram_link.py), отдельная loopback БД `telegram_profile_tests`.

## Приёмка с расхождениями (миграция 0004)

[0004_receipt_discrepancies.sql](../../migrations/documents/0004_receipt_discrepancies.sql)
добавляет `waybills.receipt_state` и `waybills_items.received_amount`. Применить **до**
обновления HTTP-портала и worker. Исходное `items.amount` сохраняется; все предложения,
отклонения и подтверждения записываются в `portal_documents_event.data.items`.
Перед редактированием сохраняется снимок `edit_previous`, включая импортированные документы.

- `POST /api/documents/waybill/{id}/receive`: `request_id`, `version`, `items`
  (ровно исходные `product_id`, фактические `amount` от 0 до 1 млрд, хотя бы одна >0).
  Требует прежнее `approve` на складе получателя. Совпадение сразу ставит документ в очередь;
  расхождение оставляет `status=Created`, `submission_state=idle`, `receipt_state=pending_sender`.
- `confirm_receipt` / `reject_receipt`: только `request_id`, `version`, только существующее
  `edit` на складе отправителя. Авторство само по себе права не даёт. При отсутствии активного
  согласующего предложение отклоняется с понятной ошибкой, не остаётся без исполнителя.
- При `pending_sender` обычные confirm/deny/edit/cancel заблокированы, в том числе Telegram.
  Подтверждение ставит единственную задачу в прежнюю очередь iiko; экспорт берёт фактические
  количества, нулевые строки исключаются. Комментарий iiko сохраняет личность получателя.
  После явного отказа iiko или сверки `absent` повторное confirm сохраняет уже согласованный
  факт. Сверка `unknown` учитывает операции confirm, receive и confirm_receipt.
- Отклонение возвращает получателю `receipt_state=rejected`, сохраняет предложенные числа
  и уведомляет получателя повторно. Можно подать исправленный факт, отклонить документ либо
  явно подтвердить исходный состав. Редактирование отправителем создаёт новую версию.
- Telegram уведомляет активных пользователей с `edit` исходного склада о расхождениях;
  кнопки и `/pending` используют те же права и версию. Отсутствие Telegram не препятствует
  согласованию на сайте. Список, карточка и результаты команд возвращают `receipt_state`;
  карточка возвращает `received_amount` и историю с `data`.

Регрессии: [test_receipt_discrepancies.py](../../tests/test_receipt_discrepancies.py).
Внешние отправки в тестах заменены заглушками; production/iiko/Telegram не используются.
