# Home Assistant: агрегаты одного ресторана

Черновик перенесён на точный GitHub base `TeamChaika/team_back`, ветка `supabase-local-cutover`, commit `101760df2b437630eb2885d19c9bd1313aba2f18`, tree `ec0fcb437d7cde5b7ff5b130ec719e80593ab462`. Четыре изменяемых исходных файла проверены byte-for-byte по GitHub blobSHA; связанная runtime/test цепочка сверена с этим tree и актуализирована в изолированной тестовой копии. Production, первоначальный локальный snapshot, БД и env не изменялись; реальные UUID и token не заполнены.

## Назначение и границы

`GET /api/integrations/home-assistant/today` выдаёт только сегодняшние агрегаты одного server-configured ресторана. Единственный allowlist состоит из `ha_bridge_department_id`. Query-параметры, даты, UUID и фильтры из запроса отвергаются HTTP422. Другие HTTP-методы не поддерживаются.

Отдельный Bearer сверяется через SHA256 и constant-time compare. Он не принимается существующими dashboard/collector-маршрутами. Cookies, `X-Sync-Key` и Supabase service-role не дают доступ к новому endpoint. Разрешения существующего профиля и ресторан перепроверяются через `Repository.scope` до и после получения данных; профиль должен быть активен, пройти штатную смену временного пароля, иметь раздел `sales` и право выбранного ресторана. Режим выбранных складов закрыт: endpoint имеет семантику целого ресторана. Можно привязать существующий профиль владельца; его остальные рестораны endpoint не выдаёт.

Новых пользователей, DB ролей, таблиц, миграций и прав на запись нет. Token не содержит ERP credentials. При отключении профиля, отзыве sales/restaurant grant или удалении server-side token hash следующий запрос отказывает даже при горячем кеше.

## Конфигурация Back Site

Поля `WebSettings` читаются с прежним префиксом `CHAIKA_WEB_`:

- `HA_BRIDGE_ENABLED`: `false` по умолчанию; выключенный маршрут возвращает 404.
- `HA_BRIDGE_TOKEN_SHA256`: 64 строчных hex символа SHA256 от отдельного случайного token. Пустое/некорректное значение при включении даёт 503.
- `HA_BRIDGE_USER_ID`: UUID существующего профиля, под чьими текущими правами выполняется чтение.
- `HA_BRIDGE_DEPARTMENT_ID`: единственный разрешённый UUID iiko подразделения Ливадии, не RMS/source ID и не имя ресторана.

Нужен существующий `CHAIKA_LIVE_SALES_ENABLED=true` и разрешённая синхронизация ресторана. HA хранит только Bearer в `secrets.yaml`; сам hash находится только в конфигурации backend. Не помещать token в URL, query, git или dashboard frontend. В этом пакете token не генерируется. Включение, заполнение UUID/секретов и публикация требуют отдельного согласованного выпуска.

## Источник и определения

Endpoint получает один `LiveSales.get(scope)` bundle, затем вызывает существующий `Repository.sales` для `daily` и `hours` с тем же bundle. Это готовый сегодняшний источник, не архивный fallback. Обновление по-прежнему выполняет общий LiveSales: он собирает свои семь утверждённых OLAP-отчётов на TTL 300–600 секунд. Endpoint не создаёт 24 запроса, не пишет бизнес-данные и не меняет источник остальных экранов.

Период — `OpenDate.Typed`, полночь–следующая полночь, `Europe/Simferopol`. Почасовая группировка — **HourOpen**, подпись графика «Продажи по часу открытия заказа». Выручка — `DishDiscountSumInt`, чеки — `UniqOrderId`, средний чек — выручка / чеки. При нулевом знаменателе средний чек `null`. Денежные значения остаются точными decimal-строками. Возвраты/знаки и утверждённые фильтры сохраняются как в существующих sales-отчётах.

Это те же daily/hours, что на странице «Продажи». Экран «Показатели» использует отдельный IndicatorService и может иметь другие фильтры/время наблюдения. Их числа не выдаются за один атомарный снимок.

## Контракт

Ответ содержит только эти поля:

```text
restaurant: {id: UUID, name: string|null}
period: {business_date: YYYY-MM-DD, start: ISO8601, end_exclusive: ISO8601,
         timezone: Europe/Simferopol, partial: true}
source: {provider: iiko_api, report: live_sales,
         date_field: OpenDate.Typed, hour_field: HourOpen}
revenue: decimal-string|null
checks: integer|null
average_check: decimal-string|null
hourly: 24 [{hour: 0..23, revenue: decimal-string|null, checks: integer|null}]
fetched_at: UTC ISO8601       # время формирования ответа, не время новой продажи
last_good: UTC ISO8601|null   # самое раннее наблюдение daily/hours валидного scoped snapshot
cache_age_seconds: integer|null
cache_seconds: integer       # существующий TTL LiveSales
is_fresh: boolean
completeness: complete|incomplete|unavailable
coverage: {status: complete|incomplete|unavailable, ...}
```

У доступного snapshot `coverage` также содержит `daily_present`, `hourly_present`, `observed_hour_count`, `reconciled`, `reconciliation_issue_count`, `observed_at`. `observed_at` описывает наблюдение источника даже при неполном scoped результате; оно не является `last_good`.

`completeness=complete` означает, что текущие daily/hours выбранного ресторана присутствуют, имеют доступные суммы/чеки и согласуются. Это не обещает закрытый день: `period.partial=true` всегда. Отсутствующие часы — `null`, не ноль. Пустой ответ, неизвестное поле и нулевой знаменатель не превращаются в нулевые продажи. У неполного результата `last_good=null`, `is_fresh=false`, все KPI и значения hourly — `null`: частичные или несогласованные суммы не попадают в графики даже при чтении атрибутов напрямую. `coverage` сохраняет сведения о присутствующих рядах и причину неполноты.

HTTP200 с `is_fresh=false` может содержать прежний успешный сегодняшний snapshot при временной ошибке iiko. Возраст и метаданные сохраняются. При достижении 1200 секунд, смене дня, отключённом LiveSales, паузе обмена, некорректном snapshot или отсутствии допустимого last-good — HTTP503 с envelope и числовыми `null`. Дата/возраст повторно проверяются после последнего permission read. Пустые, ошибочные или несогласованные наблюдения не получают новую отметку `last_good`.

401 — неверный/отсутствующий Bearer; 403 — отозванные права; 404 — выключено; 422 — параметры; 503 — конфигурация/данные недоступны. Любой ответ API имеет `Cache-Control: no-store`. Тела не содержат raw rows, requests, контрагентов, сотрудников, себестоимости или других ресторанов.

## HA

Скелет `ops/home-assistant/iiko-bridge.yaml` выполняет один REST GET раз в 300 секунд. `livadia_iiko_feed_url` — HTTPS адрес нового endpoint; `livadia_iiko_authorization` — полный `Bearer <token>`. Реальные значения намеренно отсутствуют.

REST status-сущность остаётся видимой при stale и хранит hourly/качество в атрибутах. Числовые template sensors требуют статус fresh, текущую локальную дату, неистёкший TTL и числовое значение. `now()` пересчитывает availability каждую минуту; при HTTP ошибке источник становится unavailable. Ни один шаблон не использует `float(0)`/`int(0)` для неизвестных сумм. Для этого skeleton часовой пояс HA должен быть Europe/Moscow или Europe/Simferopol. Если entity_id уже занят/переименован, заменить `sensor.livadia_iiko_source` во всех шаблонах на фактический ID.

Официальные параметры [RESTful](https://www.home-assistant.io/integrations/rest/) и [RESTful Sensor](https://www.home-assistant.io/integrations/sensor.rest/) проверены при подготовке. Skeleton не проверен командой конфигурации на действующем HA и туда не загружался.

## Проверка и следующий выпуск

`tests/test_ha_bridge.py` использует настоящие Repository.scope/sales и LiveSales, подменяя только I/O PostgreSQL/iiko. Данные/идентификаторы/token тестов синтетические. Проверяются отдельный Bearer, fixed scope владельца, запрет overrides/записи/старых маршрутов, отзыв прав до/после fetch, null/zero, stale/expiry, вчерашний snapshot и переход через полночь.

На Windows используется отдельный `run_windows_tests.py`: он лишь позволяет импортировать Linux fcntl, а любой реальный flock вызов завершает ошибкой. Этот launcher не включён в production patch. DB тесты принудительно отключены. Полный Linux/CI прогон с настоящим fcntl и изолированной тестовой PostgreSQL остаётся обязательной проверкой перед выпуском.

Перед deploy:

1. На checkout `101760df2b437630eb2885d19c9bd1313aba2f18` проверить `git rev-parse HEAD` и `git apply --check iiko-ha-bridge.patch`. При изменении base повторить перенос и проверки.
2. Подготовить draft PR в `supabase-local-cutover` и пройти штатный pull_request CI на Ubuntu + PostgreSQL17, включая `pytest` и lint. Запустить проверку конфигурации HA после заполнения secrets в штатном UI/секретном хранилище.
3. Проверить точный iiko department UUID Ливадии и профиль, согласовать выпуск backend и отдельное включение четырёх настроек. Реальный token должен быть отдельным от любых bot/ERP/sync ключей.
4. После утверждённого выпуска проверить отказ без token, фиксированный ресторан, source/freshness и совпадение daily/hours с текущей «Продажей»; затем подключить HA.
