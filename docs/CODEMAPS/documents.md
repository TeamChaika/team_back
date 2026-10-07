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

## Предварительная стоимость списаний (миграция 0005)

[0005_writeoff_cost_estimates.sql](../../migrations/documents/0005_writeoff_cost_estimates.sql)
применяется до портала/worker. Добавляет `writeoffs.cost_estimate` и узкое чтение двух таблиц
остатков из `chaika` для `chaika_iiko_app`, с RLS только источника `primary`.
Стоимость рассчитывается только при создании заявки, после проверки активного профиля,
раздела списаний и прежнего права `create` именно этого склада. В форме до создания
оценка не запрашивается. Документным пользователям аналитические права не требуются.

[app/documents/costs.py](../../app/documents/costs.py) выбирает последний полный отчёт остатков
не позднее момента оценки, максимум 48 часов давности. Суммирует повторные строки одного
товара/склада внутри одного снимка; цена = сумма остатка / количество в базовой единице.
Отрицательные количество и сумма допустимы вместе, если отношение положительное.
Нулевые, разноимённые, нечисловые остатки и отсутствующие товары не получают выдуманную цену.
Ответ: `source`, `source_at` с часовым поясом, `estimated_at`, `items` с `unit_cost`/`sum`/`reason`,
`total` (null при неполной оценке), `known_total`, `unpriced_count`. Деньги — десятичные строки;
строки округляются до копеек HALF_UP, итог складывается из округлённых строк.

Создание вычисляет и сохраняет оценку в той же транзакции. Клиентские денежные поля
запрещены. Список/карточка/результат создания возвращают `cost_estimate`; исходная оценка
не меняется при обновлении остатков. Дополнительная оценка при согласовании описана ниже. Старые карточки читают только снимки
с учётной датой и временем наблюдения не позднее создания документа; старый список возвращает
null. Это оценка по остаткам, не фактическая себестоимость проведённого iiko документа.
Нет внешних вызовов iiko, изменения payload отправки или массового заполнения истории.
Регрессии: [test_writeoff_costs.py](../../tests/test_writeoff_costs.py).

### Полуфабрикаты по сохранённым техкартам (миграция 0006)

[0006_writeoff_recipe_costs.sql](../../migrations/documents/0006_writeoff_recipe_costs.sql)
даёт runtime только SELECT источника `primary` на products, stores, corporate_nodes и три
таблицы техкарт. Применяется до обновления портала/worker; профили/складские права не меняет.
[recipe_costs.py](../../app/documents/recipe_costs.py) вызывается из оценки только если
у списываемого PREPARED нет пригодной прямой цены остатка. Вложенные PREPARED раскладываются
до GOODS, цена каждого GOODS берётся из того же снимка и выбранного склада.
Норма равна `amount_in / assembled_amount`: оба количества — в основных единицах
соответствующих товаров; упаковочные коэффициенты и нетто повторно не применяются.
Округление производится после расчёта всей строки. `items.valuation_method` сообщает
`store_balance` или `recipe`; прежние денежные поля и null неполного итога сохранены.

Подразделение определяется по предкам выбранного склада; учитываются включающие/исключающие
`store_specification` ингредиентов. Оценка стоимости производства не меняет стратегию
экспорта списания: правило direct-writeoff не запрещает оценить состав полуфабриката.
Поддерживается только COMMON без индивидуальных размеров. Отсутствующие/неоднозначные
техкарты, циклы, отсутствующая цена хотя бы одного ингредиента, неизвестная базовая единица,
неподдерживаемый тип товара или некорректные нормы оставляют строку без цены.
Техкарта должна принадлежать последнему сохранённому дню не позже оценки (не старше двух
календарных дней), действовать на дату оценки, иметь наблюдение не старше 48 часов и
не позже оценки. ID снимка заголовка и membership должны совпадать. Заголовок, membership и ингредиенты
читаются одним SQL-снимком, поэтому параллельная синхронизация не смешивает выход и нормы.
Справочники также
проверяются по времени наблюдения (48 часов); позднее перезаписанные данные для истории
не используются. Исходные сохранённые оценки, включая прежние неполные, остаются в JSON без изменения полей.
Тесты: [test_writeoff_recipe_costs.py](../../tests/test_writeoff_recipe_costs.py) и
runtime-role/исторический cutoff в [test_writeoff_costs.py](../../tests/test_writeoff_costs.py).


### Улучшение неполной оценки ожидающего списания

Карточка Created со статусом отправки idle/failed и существующей неполной оценкой
может показать текущую оценку, только если сократилось число строк без цены.
GET не записывает данные. Ответ помечен `refreshed=true`, содержит `original_estimated_at`
и отдельную `original_cost_estimate`. При равном/худшем покрытии сохраняется исходная
оценка. Полные, старые null и обработанные документы этим механизмом не пересчитываются.
Список до согласования показывает исходную оценку, без массовых запросов техкарт.

При confirm под существующей блокировкой документа выбирается оценка по тому же правилу
и сохраняется дополнительным `cost_estimate.approval_estimate` с `frozen_at_approval=true`.
Исходные поля остаются нетронутыми. Даже неполная оценка фиксируется; список, карточка и
результат confirm после этого используют approval_estimate. Повторное согласование после
ошибки отправки не пересчитывает её. Снимок записывается в событие confirm в той же
транзакции. Это предварительная стоимость, не фактическая себестоимость iiko.
Проверки: [test_writeoff_current_estimate.py](../../tests/test_writeoff_current_estimate.py).

## Удаление Telegram-уведомлений после согласования (миграция 0007)

[0007_telegram_cleanup.sql](../../migrations/documents/0007_telegram_cleanup.sql) применяется
до обновления портала и worker. Она добавляет `native_telegram_messages` (каждая успешно
доставленная страница с исходными chat_id/message_id, включая повторные `/pending`) и
`native_telegram_cleanup` (согласованная версия). Документы, сайт, статусы и история не удаляются.

`workflow.mutate` сохраняет согласование и cleanup в одной транзакции при постановке
в очередь, включая receive без расхождений и confirm_receipt. Доступность iiko не нужна;
ошибка последующей отправки не отменяет удаление. `pending_sender` ещё не является финальным
согласованием. Ожидающие уведомления согласованной версии становятся obsolete.

[telegram_cleanup.py](../../app/documents/telegram_cleanup.py) записывает ID каждой страницы
под блокировкой документа: согласование во время HTTP-отправки сразу ставит позднее сообщение
на удаление. Перед каждой страницей проверяется актуальность версии/согласования.
При сбое записи известного ID делаются три попытки записи, затем компенсирующее удаление
или снятие кнопок; фиксированный журнал с ID позволяет восстановить неудачную компенсацию.
Legacy callback регистрируется только для существующего документа допустимой версии и
его собственного inline callback_data, с проверкой диапазонов исходных идентификаторов.
Worker под существующим лидерством обрабатывает до 20 записей / 2 секунд перед iiko и
сразу после обработки Telegram callback, с сохранёнными повторами
и backoff при сетевых сбоях/5xx, `retry_after` при 429 (валидируется и ограничен сутками).
429 откладывает остальные ожидающие удаления того же чата на retry_after. Бюджет пакета
проверяется между вызовами; один сетевой вызов может длиться дольше 2 секунд. Согласование
на сайте во время уже выполняющегося iiko-вызова очищается в следующем цикле worker.
Сетевой вызов держит блокировку строки с локально отключённым idle transaction timeout;
невалидные поля ошибок Telegram не прерывают обработку очереди. Повтор удаления отсутствующего сообщения
считается успешным. Если Telegram запрещает удаление (в том числе после 48 часов), следующим
шагом убираются inline-кнопки. Ошибки доступа/невозможное редактирование отмечаются unavailable;
они не блокируют остальные записи. Текст старого сообщения в этом случае может остаться.

Миграция резервирует cleanup для уже согласованных queued/sending/sent/unknown и Sent.
Исторический `portal_documents_notification.message_id` не содержит исходный chat_id;
текущая изменяемая привязка профиля не используется для его восстановления. Миграция импортирует подлинные
chat_id/message_id из сохранённых native_bot_updates callbacks; новые callbacks также
регистрируют старое сообщение.
Неизвестные ID старых `/pending`, а также HTTP-ответы, потерянные после фактической доставки,
невозможно восстановить через Bot API. Реальные сообщения не используются для тестирования.
Регрессии: [test_telegram_cleanup.py](../../tests/test_telegram_cleanup.py), синтетическая отдельная
БД `documents_native_tests`, Telegram HTTP-заглушки.

## Глобальный предел складов

`documents.policy.stores_for` и `commercial_invoices.policy.stores_for`
пересекают прежние action grants с `chaika.portal_warehouse_access`. Ограничение
действует в native runtime и Telegram независимо от HTTP Scope. Перед выпуском
применить обе миграции и обновить worker по [контракту](../warehouse-access.md).
Глобальный склад не создаёт право действия; профили, ссылки и история сохраняются.
