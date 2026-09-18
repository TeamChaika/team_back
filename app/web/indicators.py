"""Compact daily KPIs and scoped, allowlisted OLAP filters."""

import asyncio
import json
import logging
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from typing import Annotated

import httpx
import psycopg
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.services.iiko_auth import IikoAuthService
from app.sync_references import reference_lock
from app.sync_sales_history import FixedReport, error_code
from app.web.coverage import ZONE
from app.web.live_sales import report_coverage, report_rows

log = logging.getLogger(__name__)
# Field names are from the installed iiko SALES metadata, never supplied as SQL/OLAP by clients.
FILTERS = {
    "legal_entity": ("Юридическое лицо", "JurName"),
    "concept": ("Концепция", "Conception"),
    "hall_group": ("Группа залов", "RestorauntGroup"),
    "section": ("Отделение", "RestaurantSection"),
    "register": ("Касса", "CashRegisterName"),
    "cooking_place": ("Место приготовления", "CookingPlace"),
    "payment_group": ("Группа оплаты", "PayTypes.Group"),
    "payment_type": ("Тип оплаты", "PayTypes"),
    "category": ("Категория", "DishCategory"),
    "product_type": ("Тип номенклатуры", "DishType"),
    "waiter": ("Официант заказа", "OrderWaiter.Name"),
    "product": ("Номенклатура", "DishName"),
    "delivery_type": ("Тип доставки", "Delivery.ServiceType"),
    "order_type": ("Тип заказа", "OrderType"),
    "dish_deleted": ("Блюдо удалено", "DeletedWithWriteoff"),
    "order_deleted": ("Заказ удалён", "OrderDeleted"),
    "returned": ("Возврат чека", "Storned"),
    "banquet": ("Банкет", "Banquet"),
    "weekday": ("День недели", "DayOfWeekOpen"),
    "hour_open": ("Час открытия", "HourOpen"),
    "hour_close": ("Час закрытия", "HourClose"),
}
DEFAULTS = {"dish_deleted": ["NOT_DELETED"], "order_deleted": ["NOT_DELETED"]}
BASE = {
    "quantity": "DishAmountInt",
    "checks": "UniqOrderId",
    "guests": "GuestNum",
    "revenue": "DishDiscountSumInt",
    "cost": "ProductCostBase.ProductCost",
    "discount": "DiscountSum",
}
EXTRA = {
    "return_sum": "DishReturnSum",
    "gross_revenue": "DishSumInt",
    "precheck_minutes": "OrderTime.AveragePrechequeTime",
}
DERIVED = (
    "average_check",
    "markup",
    "gross_profit",
    "discount_percent",
    "cost_share",
    "guests_per_check",
)
Value = Annotated[str, StringConstraints(min_length=1, max_length=500)]


class IndicatorQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    day: date
    filters: dict[str, list[Value]] = Field(default_factory=dict, max_length=len(FILTERS))
    direct: bool = False

    @field_validator("day")
    @classmethod
    def valid_day(cls, value):
        if not date(2000, 1, 2) <= value <= datetime.now(ZONE).date():
            raise ValueError("invalid_day")
        return value

    @field_validator("filters")
    @classmethod
    def valid_filters(cls, value):
        if set(value) - FILTERS.keys() or any(len(items) > 100 for items in value.values()):
            raise ValueError("invalid_filters")
        return {key: sorted(set(items)) for key, items in value.items()}

    def effective_filters(self):
        return {key: values for key, values in (DEFAULTS | self.filters).items() if values}

    @property
    def needs_iiko(self):
        return self.direct or self.effective_filters() != DEFAULTS


def catalog():
    return [
        {"key": key, "label": label, "default": DEFAULTS.get(key, [])}
        for key, (label, _) in FILTERS.items()
    ]


def calculate(values):
    result = {key: values.get(key) for key in BASE | EXTRA}
    revenue, cost, checks = (result[key] for key in ("revenue", "cost", "checks"))
    profit = revenue - cost if revenue is not None and cost is not None else None

    def divide(numerator, denominator, factor=1):
        return (
            numerator / denominator * factor
            if numerator is not None and denominator and denominator > 0
            else None
        )

    result.update(
        gross_profit=profit,
        average_check=divide(revenue, checks),
        markup=divide(profit, cost, 100),
        cost_share=divide(cost, revenue, 100),
        discount_percent=divide(result["discount"], result["gross_revenue"], 100),
        guests_per_check=divide(result["guests"], checks),
    )
    return result


def stored_result(day, coverage, rows):
    """Missing reports are unknown; an empty, successfully loaded report is zero."""
    output = []
    for selected in (day, day - timedelta(days=1)):
        available = {r["kind"]: r for r in coverage if r["business_date"] == selected}
        values = {}
        for kind, fields in {
            "daily": ("revenue", "cost", "checks", "guests"),
            "dishes": ("quantity",),
            "returns": ("discount", "return_sum"),
        }.items():
            members = [r for r in rows if r["business_date"] == selected and r["kind"] == kind]
            for field in fields:
                values[field] = (
                    sum((r[field] for r in members), Decimal(0))
                    if kind in available and all(r[field] is not None for r in members)
                    else None
                )
        if values.get("revenue") is not None and values.get("discount") is not None:
            values["gross_revenue"] = values["revenue"] + values["discount"]
        observations = [r["observed_at"] for r in available.values()]
        partial = any(
            t < datetime.combine(selected + timedelta(days=1), datetime.min.time(), ZONE)
            for t in observations
        )
        output.append(
            {
                "date": selected,
                "totals": calculate(values),
                "available": bool(available),
                "partial": partial,
                "observed_at": min(observations) if observations else None,
            }
        )
    return {"current": output[0], "previous": output[1], "source": "supabase"}


def read_stored(db, scope, day, live_bundle=None):
    base = (
        " FROM chaika.sales_report_days d JOIN chaika.sales_report_sets s "
        "ON s.id=d.current_set_id JOIN chaika.sales_reports r ON r.set_id=s.id "
    )
    bounds = (
        "d.source_id='primary' AND d.business_date BETWEEN %s AND %s "
        "AND r.kind IN ('daily','dishes','returns')"
    )
    dates = (day - timedelta(days=1), day)
    coverage = db.execute(
        "SELECT d.business_date,r.kind,r.observed_at" + base + "WHERE " + bounds, dates
    ).fetchall()
    # Aggregate in PostgreSQL, so neither dish volume nor the list endpoint's row cap limits KPIs.
    rows = db.execute(
        "SELECT d.business_date,r.kind,"
        + ",".join(
            f"CASE WHEN COUNT(x.{field})=COUNT(*) THEN SUM(x.{field}) END AS {field}"
            for field in (*BASE, "return_sum")
        )
        + base
        + "JOIN chaika.sales_report_rows x ON x.report_id=r.id WHERE "
        + bounds
        + " AND x.department_id=ANY(%s::uuid[]) GROUP BY d.business_date,r.kind",
        (*dates, scope.ids),
    ).fetchall()
    if live_bundle is not None:
        live_day = date.fromisoformat(live_bundle["manifest"]["business_date"])
        coverage = [r for r in coverage if r["business_date"] != live_day]
        rows = [r for r in rows if r["business_date"] != live_day]
        for kind in ("daily", "dishes", "returns"):
            coverage += report_coverage(live_bundle, [kind])
            rows += [{**r, "kind": kind} for r in report_rows(live_bundle, kind, scope)]
    return stored_result(day, coverage, rows)


def request_body(query, ids, *, fields, group="OpenDate.Typed", omit=None):
    filters = {
        FILTERS[key][1]: {"filterType": "IncludeValues", "values": values}
        for key, values in query.effective_filters().items()
        if key != omit
    }
    filters["Department.Id"] = {"filterType": "IncludeValues", "values": sorted(ids)}
    filters["OpenDate.Typed"] = {
        "filterType": "DateRange",
        "from": f"{query.day - timedelta(days=1)}T00:00:00",
        "to": f"{query.day + timedelta(days=1)}T00:00:00",
        "includeLow": True,
        "includeHigh": False,
    }
    return {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": [group],
        "groupByColFields": [],
        "aggregateFields": fields,
        "filters": filters,
    }


def parse_direct(query, reports, observed):
    result = []
    for day in (query.day, query.day - timedelta(days=1)):
        values, any_rows = {}, False
        for mapping, report in zip((BASE, EXTRA), reports, strict=True):
            matches = [r for r in report if r.get("OpenDate.Typed") == day.isoformat()]
            if len(matches) > 1:
                raise ValueError("indicators_duplicate_day")
            row = matches[0] if matches else {}
            any_rows |= bool(matches)
            for key, field in mapping.items():
                value = row.get(field)
                values[key] = (
                    Decimal(str(value))
                    if value is not None
                    else (Decimal(0) if not matches and key != "precheck_minutes" else None)
                )
                if values[key] is not None and not values[key].is_finite():
                    raise ValueError("indicators_invalid_number")
        result.append(
            {
                "date": day,
                "totals": calculate(values),
                "available": True,
                "has_sales": any_rows,
                "partial": day == datetime.now(ZONE).date(),
                "observed_at": observed,
            }
        )
    return {"current": result[0], "previous": result[1], "source": "iiko_api", "cache_seconds": 300}


def fetch_direct(settings, query, ids, option=None):
    if not ids:
        raise HTTPException(403, "Нет доступных ресторанов.")
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-indicators",
        ) as db,
        reference_lock(db),
    ):
        # Reuse the collector's lock/licence convention; each batch logs out in finally.
        try:
            response = httpx.post(
                "http://127.0.0.1:8010/api/v1/iiko/connections/primary/logout",
                timeout=10,
                trust_env=False,
            )
            response.raise_for_status()
            if response.json().get("state") != "logged_out":
                raise ValueError("indicators_logout_failed")
        except httpx.ConnectError:
            pass
        with TemporaryDirectory(prefix="chaika-indicators-") as temporary:
            root = Path(temporary)

            async def collect():
                client = IikoClient(settings)
                auth = IikoAuthService(settings, client)
                try:
                    bodies = (
                        [
                            request_body(
                                query,
                                ids,
                                fields=["UniqOrderId"],
                                group=FILTERS[option][1],
                                omit=option,
                            )
                        ]
                        if option
                        else [
                            request_body(query, ids, fields=list(mapping.values()))
                            for mapping in (BASE, EXTRA)
                        ]
                    )
                    results = []
                    for index, body in enumerate(bodies):
                        path = root / f"{index}.json"
                        await auth.download_daily_sales(path, query=FixedReport(body))
                        payload = json.loads(path.read_bytes(), parse_float=Decimal)
                        if not isinstance(payload.get("data"), list):
                            raise ValueError("indicators_invalid_data")
                        results.append(payload["data"])
                    return results
                finally:
                    try:
                        try:
                            if (await auth.logout()).state != "logged_out":
                                raise ValueError("indicators_logout_failed")
                        except Exception as error:
                            raise IikoError(
                                "iiko_session_unknown",
                                "Не удалось подтвердить выход из iiko.",
                                status_code=503,
                                outcome_unknown=True,
                            ) from error
                    finally:
                        await client.aclose()

            reports = asyncio.run(collect())
    if option:
        field = FILTERS[option][1]
        values = {str(r[field]) for r in reports[0] if r.get(field) is not None}
        return {"values": sorted(values, key=str.casefold)}
    return parse_direct(query, reports, datetime.now(UTC))


class IndicatorService:
    def __init__(self, settings, *, fetch=fetch_direct, clock=monotonic):
        self.settings, self.fetch, self.clock = settings, fetch, clock
        self.lock, self.cache = Lock(), OrderedDict()
        self.session_unknown = False
        self.retry_after = 0.0

    def get(self, scope, query, option=None):
        if option is not None and option not in FILTERS:
            raise HTTPException(422, "Неизвестный фильтр.")
        ids = sorted(str(value) for value in scope.ids)
        if not ids:
            raise HTTPException(403, "Нет доступных ресторанов.")
        key = json.dumps(
            [ids, query.day.isoformat(), query.effective_filters(), option], sort_keys=True
        )
        if not self.lock.acquire(timeout=2):
            raise HTTPException(
                503, "iiko формирует отчёт. Повторите запрос через несколько секунд."
            )
        try:
            cached = self.cache.get(key)
            if cached and cached[0] > self.clock():
                self.cache.move_to_end(key)
                return cached[1]
            if self.session_unknown:
                raise HTTPException(
                    503, "Не удалось подтвердить выход из iiko. Нужна проверка соединения."
                )
            if self.clock() < self.retry_after:
                raise HTTPException(503, "iiko временно недоступна. Повторите через минуту.")
            try:
                result = self.fetch(self.settings, query, ids, option)
            except HTTPException:
                raise
            except Exception as error:
                log.warning("Indicator OLAP unavailable: %s", error_code(error))
                self.retry_after = self.clock() + 30
                self.session_unknown = error_code(error) == "iiko_session_unknown"
                raise HTTPException(
                    503,
                    "iiko пока не ответила. Выбранные фильтры не применены. "
                    "Повторите запрос позже.",
                ) from None
            self.cache[key] = (self.clock() + 300, result)
            self.cache.move_to_end(key)
            while len(self.cache) > 100:
                self.cache.popitem(last=False)
            return result
        finally:
            self.lock.release()
