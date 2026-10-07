"""Warehouse-authorized historical SALES, captured from iiko for the requested period.

Store.Id is an immutable IncludeValues filter, never a grouping dimension: a
check containing dishes from two allowed stores is counted once by iiko.
Department-only saved snapshots cannot supply this read path.
"""

import logging
from collections import OrderedDict
from copy import deepcopy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from threading import Lock
from time import monotonic
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg
from fastapi import HTTPException

from app.import_sales_review import METRICS
from app.sync_sales_history import approved_templates, error_code
from app.web.indicators import collect_reports
from app.web.overview import build_overview, dates
from app.web.warehouse_analytics import warehouse_ids

log = logging.getLogger(__name__)
KINDS = frozenset(("daily", "dishes", "payments", "discounts", "returns", "waiters", "hours"))


def scoped_request(template, scope, start, end):
    stores = warehouse_ids(scope)
    if stores is None or not scope.ids:
        raise HTTPException(403, "Нет доступных складов или заведений.")
    body = deepcopy(template)
    # Replace even a pre-existing template scope, after all other filters.
    body["filters"]["Store.Id"] = {"filterType": "IncludeValues", "values": list(stores)}
    body["filters"]["Department.Id"] = {
        "filterType": "IncludeValues",
        "values": sorted(str(value) for value in scope.ids),
    }
    body["filters"]["OpenDate.Typed"] = {
        "filterType": "DateRange",
        "from": f"{start}T00:00:00",
        "to": f"{end + timedelta(days=1)}T00:00:00",
        "includeLow": True,
        "includeHigh": False,
    }
    groups = body["groupByRowFields"]
    if (
        body["reportType"] != "SALES"
        or body["buildSummary"] is not False
        or body.get("groupByColFields")
        or not {"OpenDate.Typed", "Department.Id"}.issubset(groups)
        or "Store.Id" in groups
        or "Store.Name" in groups
    ):
        raise ValueError("sales_warehouse_template_invalid")
    return body


def parse_rows(raw_rows, body, scope, start, end, observed, kind):
    """Validate provider boundaries and metrics before exposing any report rows."""
    visible = {str(value) for value in scope.ids}
    names = {str(item["id"]): item.get("name") for item in scope.departments}
    result, seen = [], set()
    identity = uuid5(NAMESPACE_URL, repr(body) + observed.isoformat())
    for ordinal, raw in enumerate(raw_rows):
        day = date.fromisoformat(raw["OpenDate.Typed"])
        department = raw["Department.Id"]
        if not start <= day <= end or department not in visible:
            raise ValueError("sales_warehouse_provider_scope_mismatch")
        dimensions = {key: raw[key] for key in body["groupByRowFields"]}
        group = tuple(dimensions.items())
        if group in seen:
            raise ValueError("sales_warehouse_duplicate_group")
        seen.add(group)
        for field in body["aggregateFields"]:
            value = raw[field]
            if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
                raise ValueError("sales_warehouse_invalid_metric")
            if not Decimal(value).is_finite():
                raise ValueError("sales_warehouse_invalid_metric")
        if (
            kind == "discounts"
            and not str(raw.get("ItemSaleEventDiscountType") or "").strip()
            and not raw.get("DiscountSum")
        ):
            continue
        result.append(
            {
                "business_date": day,
                "report_id": identity,
                "ordinal": ordinal,
                "department_id": UUID(department),
                "department": names.get(department),
                "observed_at": observed,
                "request": deepcopy(body),
                "reviewed": False,
                "live": True,
                "warehouse_scoped": True,
                "drilldown_available": kind == "discounts",
                **{
                    key: Decimal(raw[field]) if raw.get(field) is not None else None
                    for key, field in METRICS.items()
                },
                "dimensions": dimensions,
            }
        )
    return result


def fetch_reports(settings, scope, start, end, kinds):
    with psycopg.connect(
        settings.database_url.get_secret_value(), autocommit=True, connect_timeout=10
    ) as db:
        _, templates = approved_templates(db)
    bodies = [scoped_request(templates[kind], scope, start, end) for kind in kinds]
    reports = collect_reports(settings, bodies)
    if len(reports) != len(kinds):
        raise ValueError("sales_warehouse_incomplete_capture")
    observed = datetime.now(UTC)
    rows = {
        kind: parse_rows(raw, body, scope, start, end, observed, kind)
        for kind, body, raw in zip(kinds, bodies, reports, strict=True)
    }
    coverage = [
        {
            "business_date": day,
            "kind": kind,
            "reviewed": False,
            "checks": [],
            "observed_at": observed,
        }
        for day in dates(start, end)
        for kind in kinds
    ]
    return {"rows": rows, "coverage": coverage, "observed_at": observed}


class WarehouseSales:
    def __init__(self, settings, *, fetch=fetch_reports, clock=monotonic):
        self.settings, self.fetch, self.clock = settings, fetch, clock
        self.lock, self.cache = Lock(), OrderedDict()
        self.contexts = OrderedDict()

    def get(self, scope, start, end, kinds):
        stores = warehouse_ids(scope)
        if stores is None or not scope.ids:
            raise HTTPException(403, "Нет доступных складов или заведений.")
        kinds = tuple(sorted(set(kinds)))
        if not kinds or not set(kinds).issubset(KINDS):
            raise HTTPException(404, "Отчёт не найден.")
        # User identity and effective grants isolate captures after every permission change.
        key = (
            str(scope.user.get("id")),
            tuple(sorted(str(i) for i in scope.ids)),
            stores,
            start,
            end,
            kinds,
        )
        with self.lock:
            cached = self.cache.get(key)
            if cached and cached[0] > self.clock():
                self.cache.move_to_end(key)
                return deepcopy(cached[1])
            try:
                capture = self.fetch(self.settings, scope, start, end, kinds)
            except Exception as error:
                log.warning("Warehouse SALES unavailable: %s", error_code(error))
                raise HTTPException(
                    503, "Данные iiko по выбранным складам пока недоступны. Повторите запрос позже."
                ) from None
            self.cache[key] = (self.clock() + 300, deepcopy(capture))
            discount_rows = capture["rows"].get("discounts", [])
            if discount_rows:
                report_id = discount_rows[0]["report_id"]
                self.contexts[report_id] = {
                    "expires": self.clock() + 1800,
                    "access": key[:3],
                    "rows": {row["ordinal"]: deepcopy(row) for row in discount_rows},
                    "captures": {},
                }
                while len(self.contexts) > 32:
                    self.contexts.popitem(last=False)
            self.cache.move_to_end(key)
            while len(self.cache) > 32:
                self.cache.popitem(last=False)
            return capture

    def sales(self, scope, kind, start, end, *, dish_id=None, dish_name=None):
        capture = self.get(scope, start, end, (kind,))
        rows = capture["rows"][kind]
        if dish_id is not None:
            rows = [row for row in rows if row["dimensions"].get("DishId") == dish_id]
        if dish_name is not None:
            rows = [
                row
                for row in rows
                if not row["dimensions"].get("DishId")
                and row["dimensions"].get("DishName", "") == dish_name
            ]
        if len(rows) > 20000:
            raise HTTPException(
                413, "Слишком много строк. Выберите меньший период или один ресторан."
            )
        return capture["coverage"], rows

    def overview(self, scope, start, end, grain):
        previous_start = start - timedelta(days=(end - start).days + 1)
        capture = self.get(scope, previous_start, end, ("daily", "dishes"))
        grouped = {}
        with localcontext() as ctx:
            ctx.prec = 70
            for row in capture["rows"]["dishes"]:
                dish_id = row["dimensions"].get("DishId") or None
                name = row["dimensions"].get("DishName")
                fallback = (name or "") if dish_id is None else None
                current = row["business_date"] >= start
                key = (current, dish_id, fallback)
                target = grouped.setdefault(
                    key,
                    {
                        "is_current": current,
                        "dish_id": dish_id,
                        "dish_name": name,
                        "fallback_name": fallback,
                        "revenue": Decimal(0),
                        "quantity": Decimal(0),
                    },
                )
                target["dish_name"] = name
                for metric in ("revenue", "quantity"):
                    target[metric] = (
                        target[metric] + row[metric]
                        if target[metric] is not None and row[metric] is not None
                        else None
                    )
        return build_overview(
            scope,
            start,
            end,
            grain,
            capture["coverage"],
            capture["rows"]["daily"],
            list(grouped.values()),
        )

    def discount_details(self, scope, query):
        """Drill into an immutable scoped capture, never into a whole-venue row."""
        from app.services.sales_drilldown import PAGE_SIZE, parse_rows, query_for, reconcile

        stores = warehouse_ids(scope)
        access = (str(scope.user.get("id")), tuple(sorted(str(i) for i in scope.ids)), stores)
        with self.lock:
            saved = self.contexts.get(query.report_id)
            if saved is None or saved["expires"] <= self.clock():
                raise HTTPException(410, "Срок снимка истёк. Обновите отчёт по скидкам.")
            if saved["access"] != access or query.ordinal not in saved["rows"]:
                raise HTTPException(404, "Строка скидки не найдена или недоступна.")
            row = saved["rows"][query.ordinal]
            context = {
                **row,
                "discount_name": row["dimensions"]["ItemSaleEventDiscountType"],
                "allowed_store_ids": list(stores),
            }

            def capture(order_id=None):
                key = (query.ordinal, str(order_id) if order_id else None)
                if key not in saved["captures"]:
                    try:
                        request = query_for(context, order_id)
                        reports = collect_reports(self.settings, [request])
                        if len(reports) != 1:
                            raise ValueError("sales_warehouse_incomplete_capture")
                        rows = parse_rows(
                            {"data": reports[0], "summary": []}, request, context, order_id
                        )
                        saved["captures"][key] = {
                            "id": uuid5(query.report_id, repr(key)),
                            "rows": rows,
                            "observed_at": datetime.now(UTC),
                        }
                    except Exception as error:
                        if isinstance(error, HTTPException):
                            raise
                        log.warning("Warehouse discount detail unavailable: %s", error_code(error))
                        raise HTTPException(
                            503, "Детализация iiko по выбранным складам пока недоступна."
                        ) from None
                return deepcopy(saved["captures"][key])

            orders = capture()
            selected, data = None, orders
            if query.order_id is not None:
                selected = next(
                    (r for r in orders["rows"] if r["order_id"] == str(query.order_id)), None
                )
                if selected is None:
                    raise HTTPException(404, "Заказ отсутствует в этой скидке.")
                selected.update(topology=None, events_state="unavailable")
                data = capture(query.order_id)
            check = reconcile(
                data["rows"],
                selected or context,
                context["discount_name"],
                items=selected is not None,
            )
            rows = data["rows"][query.offset : query.offset + PAGE_SIZE]
            if selected is None:
                for item in rows:
                    item.update(topology=None, events_state="unavailable")
            return {
                "kind": "items" if selected else "orders",
                "capture_id": data["id"],
                "observed_at": data["observed_at"],
                "department": context["department"],
                "business_date": context["business_date"],
                "discount_name": context["discount_name"],
                "selected_order": selected,
                "rows": rows,
                "total": len(data["rows"]),
                "offset": query.offset,
                "limit": PAGE_SIZE,
                "reconciliation": check,
            }
