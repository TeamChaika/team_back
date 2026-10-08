# Tenant dashboard: первый read-only срез

Локальная реализация 08.10.2026. Это существующий frontend dashboard с tenant API,
а не отдельная упрощённая аналитическая страница. Публикация и сверка с реальным iiko
подтверждаются отдельно.

## Контракт

Префикс: `/api/saas-tenant/{slug}/dashboard`.

- `GET /me`: обычный dashboard Meta. Настоящие departments из своей Chain,
  `sections=[overview,sales]`, `supported_sales_kinds=[daily,dishes]`, роль
  `company_admin`, `can_manage=false`, `documents_enabled=false`. `sales_dates=[]`
  не подменяется выдуманной историей; `today` — Europe/Simferopol. Настройка часового
  пояса произвольной компании пока не реализована.
- `GET /overview?start&end&department_id&granularity`: прежний OverviewData,
  периоды current/previous, trends day/week/month, restaurants, top_dishes.
- `GET /sales/{daily|dishes}?start&end&department_id&dish_id|dish_name`: прежний Sales.
  `department_id` повторяется для нескольких UUID. Один фильтр блюда допускается
  только для dishes. Период до 31 дня, не позднее today, до 100 заведений.
- Ответы содержат `data_status`: source=iiko_api, status=ready, observed_at,
  expires_at, cache_seconds=300, timezone. Ошибки возвращаются как 4xx/503,
  не становятся успешным пустым отчётом. Историческая репликация Chain может отставать;
  complete означает успешное получение всего запрошенного периода, не сверку RMS.

## Границы данных и доступа

`pg_tenant_access.tenant_dashboard_source` вызывает существующий `_verified`:
Supabase identity, активное membership, компания/slug, отсутствие архива/блокировки.
Дополнительно нужны active company, analytics module и сменённый временный пароль.
Эти проверки выполняются до cache hit и после завершения чтения iiko. Изменение
настроек во время чтения закрывает ответ с source_changed.

Источник — только connection_id=chain этой компании и точное совпадение её сохранённого
URL. Credentials расшифровываются существующим vault. Нет fallback в CHAIKA_*,
`.env`, RMS, app.portal, chaika schema, SQLite или чужой dashboard. Никаких записей
бизнес-данных и создания SQL-слоя аналитики в этом срезе нет.

`dashboard_transport` выполняет только departments/columns/SALES, auth/logout.
TLS с проверкой имени, pinned public DNS, HTTPS443, ограниченный path, отсутствие
redirects, лимиты размера и времени. Одна сессия на последовательный пакет запросов,
подтверждённый logout в finally; без подтверждения snapshot не выдаётся.

## Агрегирование и кэш

`dashboard_reports` валидирует XML, UUID, дубли, циклы, JSON, summary, область
заведений/дат, конечные Decimal. Используются текущие SALES column capabilities.
По умолчанию исключены удалённые блюда/заказы; возвраты дополнительно не исключаются.
Нет суммирования Chain+RMS.

`app.web.overview.build_overview/totals/changes` переиспользуются как чистые функции.
Общие checks/guests/average_check приходят из отдельного негруппированного OLAP
за весь период и выбранное объединение заведений. В трендах и ресторанных подытогах
checks/guests остаются null; дробный GuestNum допустим. Dishes не выдаёт счётчики
посетителей/чеков. Наценка = (выручка−себестоимость)/себестоимость×100 при положительной
себестоимости. Отсутствующее поле — null; успешное пустое data — нулевая сумма.

Кэш процесса: 300 секунд, до 64 записей/32 MiB, до 16 MiB на запись. Ключ включает
company UUID, версию, fingerprint ciphertext+URL, тип/версию отчёта, период и фильтры.
Запросы одного hostname сериализуются, глобально одновременно не более 4 пакетов.
После ошибок действует cooldown источника, не только одного фильтра; после timeout
или неизвестного logout — 300 секунд. Поддерживаемый runtime — один HTTP worker;
перед несколькими worker нужны межпроцессные lease/cache.

## Поставка и проверка

Изолированный SaaS runtime должен включать `app/saas_admin`, а также чистые
`app/web/{__init__,overview,coverage}.py`. `overview.read_overview` не вызывается;
его SQL-путь и остальные app/web модули не подключаются. Дополнительная зависимость
`defusedxml==0.7.1` добавлена в requirements-saas-admin.txt.

Проверки: `tests/test_saas_tenant_dashboard.py`, прежние SaaS auth/domain/transport
tests. Синтетические компании используют одинаковые iiko UUID и разные суммы;
проверяются отзыв доступа при cache hit/во время загрузки, запрет чужого заведения,
изменение credentials, неправильные источники/XML/JSON, SSRF boundaries,
подтверждённый logout и неизвестные показатели. Реальный upstream не вызывается.
