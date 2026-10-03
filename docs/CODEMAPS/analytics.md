# Аналитика, показатели и закупки

Срез: 2026-10-03, Git `7c48c412d5f8c225af34c99193206507fae4f0f1`. Ключевая граница — сохранённая история PostgreSQL и прямые снимки iiko за сегодня.

| Задача | Файлы и путь вызова |
| --- | --- |
| Продажи/обзор | [app/portal.py](../../app/portal.py) `/api/sales/{kind}`, `/api/overview` → [app/web/repository.py](../../app/web/repository.py) и [app/web/overview.py](../../app/web/overview.py) → `chaika.sales_report_days/reports/rows`; выдача учитывает `Scope.ids` и полноту дней |
| Текущий день | [app/web/live_sales.py](../../app/web/live_sales.py) собирает OLAP через [app/sync_sales_history.py](../../app/sync_sales_history.py) и [app/import_sales_review.py](../../app/import_sales_review.py); `LiveSales.get` держит снимок в памяти и заменяет только сегодняшний день в выдаче. Ответ имеет `source=iiko_api`, дату, `stale` и TTL; после допустимого срока устаревания возвращается 503 |
| Показатели | `/api/indicators/query` и `/metric/{metric}` → [app/web/indicators.py](../../app/web/indicators.py) `IndicatorService` → прямой OLAP iiko и кеш. Их суммы не читаются из сохранённых отчётов продаж; временное расхождение возможно из-за источника, фильтров и времени наблюдения |
| Значения фильтров | [app/sync_indicator_filters.py](../../app/sync_indicator_filters.py) собирает варианты OLAP в `chaika.indicator_filter_values` и статус в `indicator_filter_sync`; [app/web/repository.py](../../app/web/repository.py) выдаёт варианты только для `Scope.ids` |
| Закупочная цена | `/api/purchase-prices` → [app/web/purchase_prices.py](../../app/web/purchase_prices.py): обработанные приходные строки, история и прежняя цена по продукту/складу/единице; связанные расходные накладные учитываются отдельно |
| Недельный прогноз | `include_impact=true` → [app/web/purchase_impact_summary.py](../../app/web/purchase_impact_summary.py) загружает один набор цен, техкарт и продаж за 30 завершённых дней → [app/web/purchase_impact.py](../../app/web/purchase_impact.py) считает `weekly_delta`; неполные продажи блокируют недельную оценку |
| Одна позиция | `/api/purchase-prices/impact` → [app/web/purchase_impact.py](../../app/web/purchase_impact.py): цепочка техкарт с подразделением, размером, прямым списанием и проверкой неоднозначности. Это сценарная оценка, не фактическое списание |
| Детали скидки | `/api/discount-details` → [app/services/sales_drilldown.py](../../app/services/sales_drilldown.py), отдельное чтение iiko с проверкой выбранных подразделений |
| Документы/остатки/события | `/api/resources/{resource}` → `resource_query`/`resources`/`detail` в [app/web/repository.py](../../app/web/repository.py), списки из `chaika.*`; `/api/topology` проверяет `rms_id` и идёт в [app/services/order_topology.py](../../app/services/order_topology.py) |

При пустом сегодняшнем отчёте начните с `LiveSales.get` и метаданных ответа. Отсутствие строки в PostgreSQL не доказывает отсутствие продажи в iiko. В показателях сохранённый справочник вариантов фильтров не означает, что прямой OLAP-запрос завершился.

Проверки: [test_live_sales.py](../../tests/test_live_sales.py), [test_sales_coverage.py](../../tests/test_sales_coverage.py), [test_indicator_filters.py](../../tests/test_indicator_filters.py), [test_purchase_prices.py](../../tests/test_purchase_prices.py), [test_purchase_impact.py](../../tests/test_purchase_impact.py).
