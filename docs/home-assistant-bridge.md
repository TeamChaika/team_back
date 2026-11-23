# Home Assistant: агрегаты отображаемого дня одного ресторана

Изменение правила выбора даты подготовлено относительно GitHub base `TeamChaika/team_back`, ветка `supabase-local-cutover`, commit `c2e8832f8b68efadda978b7fbb6f2157cd6a8ab1`, tree `1c418aa2074f978d401269f30c3b6c6188e50f1e`. Изменяемые исходные файлы проверены по GitHub blobSHA. В этой подготовке production, БД и env не менялись; существующие owner/hash/UUID и HA entity IDs сохраняются.

## Назначение и границы

`GET /api/integrations/home-assistant/today` сохраняет прежний URL и выдаёт агрегаты одного server-configured ресторана за дату, выбранную сервером. В `Europe/Simferopol` (UTC+3) с00:00 до08:59:59 выбирается **вчерашний календарный день**, с09:00 до23:59:59 — **текущий**. Формула: `(local_now - timedelta(hours=9)).date()`. Сам период всегда00:00–следующая00:00 выбранного дня: это не скользящие24часа и не сдвиг границ отчёта на09:00. Единственный allowlist состоит из `ha_bridge_department_id`. Query-параметры, даты, UUID и фильтры из запроса отвергаются HTTP422. Другие HTTP-методы не поддерживаются.

Отдельный Bearer сверяется через SHA256 и constant-time compare. Он не принимается существующими dashboard/collector-маршрутами. Cookies, `X-Sync-Key` и Supabase service-role не дают доступ к новому endpoint. Разрешения существующего профиля и ресторан перепроверяются через `Repository.scope` до и после получения данных; профиль должен быть активен, пройти штатную смену временного пароля, иметь раздел `sales` и право выбранного ресторана. Режим выбранных складов закрыт: endpoint имеет семантику целого ресторана. Можно привязать существующий профиль владельца; его остальные рестораны endpoint не выдаёт.

Новых пользователей, DB ролей, таблиц, миграций и прав на запись нет. Token не содержит ERP credentials. При отключении профиля, отзыве sales/restaurant grant или удалении server-side token hash следующий запрос отказывает даже при горячем кеше.

## Конфигурация Back Site

Поля `WebSettings` читаются с прежним префиксом `CHAIKA_WEB_`:

- `HA_BRIDGE_ENABLED`: `false` по умолчанию; выключенный маршрут возвращает 404.
- `HA_BRIDGE_TOKEN_SHA256`: 64 строчных hex символа SHA256 от отдельного случайного token. Пустое/некорректное значение при включении даёт 503.
- `HA_BRIDGE_USER_ID`: UUID существующего профиля, под чьими текущими правами выполняется чтение.
- `HA_BRIDGE_DEPARTMENT_ID`: единственный разрешённый UUID iiko подразделения Ливадии, не RMS/source ID и не имя ресторана.

Нужен существующий `CHAIKA_LIVE_SALES_ENABLED=true` и разрешённая синхронизация ресторана. HA хранит только Bearer в `secrets.yaml`; сам hash находится только в конфигурации backend. Не помещать token в URL, query, git или dashboard frontend. Изменение даты не требует нового token, пользователя, UUID, настроек или прав; применяется к существующему подключению при обычном выпуске.

## Источник и определения

Endpoint получает один `LiveSales.for_day(selected_date).get(scope)` bundle, затем вызывает существующий `Repository.sales` для `daily` и `hours` с тем же bundle. После09:00 используется прежний общий кеш сегодняшнего дня. До09:00 создаётся отдельный кеш с неизменяемой датой вчерашнего дня; `fetch_day(settings, selected_date)` передаёт именно эту дату в существующий `collect_day`/`parse_review`. Обычные `LiveSales.get(scope)` остальных экранов продолжают возвращать сегодня. В памяти остаются основной кеш и не более одного отдельного previous-day кеша; при следующем дне старый previous-day заменяется.

Оба кеша используют прежний PostgreSQL `reference_lock`, семь утверждённых OLAP-отчётов, подтверждённый logout, TTL300–600секунд и ограниченный last-good до1200секунд. Дата вчерашнего capture не меняется, даже если за время получения наступили новые сутки. Снимок вчера должен быть получен не раньше следующей местной00:00; предшествующий полуночи снимок нельзя объявить полным вчерашним. Последние права, правило выбранной даты и возраст проверяются после чтения. Если запрос пересёк09:00 и выбранная дата сменилась, ответ503 с новой ожидаемой датой и числовыми null.

Сохранённая история БД не используется как неподтверждённый fallback. Сразу после00:00 финального вчерашнего поколения может ещё не быть (плановые продажи собираются02:00/06:00); отдельные `repo.sales` также не доказывают общий generation, отсутствие frozen строк и полноту выбранного UUID. Надёжный DB-first потребовал бы отдельного доказанного reader и всё равно dated capture при отсутствии финальной истории. Текущий вариант повторно использует готовый безопасный pipeline без новых таблиц, ролей и бизнес-записей. Если днём другие dashboard используют today-кеш одновременно с ночным dated-кешем, сборы сериализуются тем же mutex; занятость даёт обычный bounded fallback/503, без принудительного unlock и увеличения TTL.

Период — `OpenDate.Typed`, полночь–следующая полночь, `Europe/Simferopol`. Почасовая группировка — **HourOpen**, подпись графика «Продажи по часу открытия заказа». Выручка — `DishDiscountSumInt`, чеки — `UniqOrderId`, средний чек — выручка / чеки. При нулевом знаменателе средний чек `null`. Денежные значения остаются точными decimal-строками. Возвраты/знаки и утверждённые фильтры сохраняются как в существующих sales-отчётах.

Это те же daily/hours, что на странице «Продажи». Экран «Показатели» использует отдельный IndicatorService и может иметь другие фильтры/время наблюдения. Их числа не выдаются за один атомарный снимок.

## Контракт

Ответ содержит только эти поля:

```text
restaurant: {id: UUID, name: string|null}
period: {business_date: YYYY-MM-DD, start: ISO8601, end_exclusive: ISO8601,
         timezone: Europe/Simferopol, partial: boolean}
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

`completeness=complete` означает, что daily/hours выбранного ресторана присутствуют, имеют доступные суммы/чеки и согласуются. `period.partial=false` до09:00, когда показывается завершённый календарный день, и `true` с09:00 для текущего дня. Это не утверждает закрытие кассовой смены или отсутствие последующих исправлений: вчерашний отчёт тоже обновляется из iiko. Отсутствующие часы — `null`, не ноль. Пустой ответ, неизвестное поле и нулевой знаменатель не превращаются в нулевые продажи. У неполного результата `last_good=null`, `is_fresh=false`, все KPI и значения hourly — `null`: частичные или несогласованные суммы не попадают в графики даже при чтении атрибутов напрямую. `coverage` сохраняет сведения о присутствующих рядах и причину неполноты.

HTTP200 с `is_fresh=false` может содержать прежний успешный snapshot **той же выбранной даты** при временной ошибке iiko. Возраст и метаданные сохраняются; `last_good`/`coverage.observed_at` всегда описывают реальное получение iiko, даже для вчера. При достижении1200секунд, смене отображаемой даты, отключённом LiveSales, паузе обмена, некорректном snapshot или отсутствии допустимого last-good — HTTP503 с envelope и числовыми `null`. Дата/возраст повторно проверяются после последнего permission read. Пустые, ошибочные или несогласованные наблюдения не получают новую отметку `last_good`.

401 — неверный/отсутствующий Bearer; 403 — отозванные права; 404 — выключено; 422 — параметры; 503 — конфигурация/данные недоступны. Любой ответ API имеет `Cache-Control: no-store`. Тела не содержат raw rows, requests, контрагентов, сотрудников, себестоимости или других ресторанов.

## HA

Скелет `ops/home-assistant/iiko-bridge.yaml` выполняет один REST GET раз в 300 секунд. `livadia_iiko_feed_url` — HTTPS адрес нового endpoint; `livadia_iiko_authorization` — полный `Bearer <token>`. Реальные значения намеренно отсутствуют.

REST status-сущность остаётся видимой при stale и хранит hourly/качество в атрибутах. Числовые template sensors требуют статус fresh, ожидаемую дату `(now()-timedelta(hours=9)).date()`, неистёкший TTL и числовое значение. `now()` пересчитывает availability каждую минуту; при HTTP ошибке источник становится unavailable. Ни один шаблон не использует `float(0)`/`int(0)` для неизвестных сумм. Часовой пояс HA должен быть Europe/Moscow или Europe/Simferopol. Идентификаторы с `_today` и unique_id сохраняются для существующих dashboard; подписи — «Выручка», «Чеки», «Средний чек», дата выводится из `period.business_date`. Если entity_id уже занят/переименован, использовать фактический ID реестра.

Официальные параметры [RESTful](https://www.home-assistant.io/integrations/rest/) и [RESTful Sensor](https://www.home-assistant.io/integrations/sensor.rest/) проверены при подготовке. Skeleton не проверен командой конфигурации на действующем HA и туда не загружался.

## Проверка и следующий выпуск

`tests/test_ha_bridge.py` использует настоящие Repository.scope/sales и LiveSales, подменяя только I/O PostgreSQL/iiko. `tests/test_ha_bridge_period.py` покрывает00:00/08:59:59/09:00/23:59:59, UTC→MSK, смену месяца/года и29февраля, реальную передачу даты в capture под mutex, отдельный yesterday-кеш, cache-hit/failure/expiry без подстановки today и09:00 во время запроса. Данные/идентификаторы/token тестов синтетические. Прежние проверки Bearer, scope, отзыва прав и null/zero сохраняются.

На Windows используется отдельный `run_windows_tests.py`: он лишь позволяет импортировать Linux fcntl, а любой реальный flock вызов завершает ошибкой. Этот launcher не включён в production patch. DB тесты принудительно отключены. Полный Linux/CI прогон с настоящим fcntl и изолированной тестовой PostgreSQL остаётся обязательной проверкой перед выпуском.

Перед deploy:

1. На checkout `c2e8832f8b68efadda978b7fbb6f2157cd6a8ab1` проверить `git rev-parse HEAD` и `git apply --check iiko-period.patch`. При изменении base повторить перенос и проверки.
2. Подготовить draft PR в `supabase-local-cutover` и пройти штатный pull_request CI на Ubuntu + PostgreSQL17, включая `pytest` и lint. Запустить проверку конфигурации HA после заполнения secrets в штатном UI/секретном хранилище.
3. Выпустить backend с прежними owner/hash/UUID/settings. В HA обновить guards на09:00 правило и подписи, сохранив entity IDs. До согласования двух сторон ночные значения могут временно быть unavailable; старую дату нельзя насильно считать текущей.
4. После выпуска использовать поддерживаемые `rest.reload`/`template.reload` и обновить raw source, без дополнительного Core restart. Проверить выбранную дату, source timestamps, null/empty и совпадение daily/hours с «Продажами» за эту календарную дату.
