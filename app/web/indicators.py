"""Independent, cached iiko indicators. No sales totals are read from PostgreSQL."""

import asyncio
import json
import logging
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
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
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from app.integrations.iiko.client import IikoClient
from app.integrations.iiko.errors import IikoError
from app.services.iiko_auth import IikoAuthService
from app.sync_references import reference_lock
from app.sync_sales_history import FixedReport, error_code
from app.web.coverage import ZONE

log = logging.getLogger(__name__)
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
    start: date | None = None
    end: date | None = None
    previous_start: date | None = None
    previous_end: date | None = None
    day: date | None = None  # Compatibility with the first daily frontend.
    direct: bool = True
    filters: dict[str, list[Value]] = Field(default_factory=dict, max_length=len(FILTERS))

    @model_validator(mode="after")
    def valid_periods(self):
        if self.day and (self.start or self.end):
            raise ValueError("Choose day or range")
        self.start, self.end = self.start or self.day, self.end or self.day
        if not self.start or not self.end:
            raise ValueError("Period is required")
        today = datetime.now(ZONE).date()
        if not date(2000, 1, 2) <= self.start <= self.end <= today:
            raise ValueError("Invalid period")
        if (self.end - self.start).days > 1826:
            raise ValueError("Period exceeds five years")
        if bool(self.previous_start) != bool(self.previous_end):
            raise ValueError("Both comparison dates are required")
        if not self.previous_start:
            self.previous_end = self.start - timedelta(days=1)
            self.previous_start = self.previous_end - (self.end - self.start)
        if not date(2000, 1, 1) <= self.previous_start <= self.previous_end < self.start:
            raise ValueError("Invalid comparison period")
        if (self.previous_end - self.previous_start).days > 1826:
            raise ValueError("Comparison exceeds five years")
        return self

    @field_validator("filters")
    @classmethod
    def valid_filters(cls, value):
        if set(value) - FILTERS.keys() or any(len(items) > 100 for items in value.values()):
            raise ValueError("invalid_filters")
        return {key: sorted(set(items)) for key, items in value.items()}

    def effective_filters(self):
        return {key: values for key, values in (DEFAULTS | self.filters).items() if values}


def catalog():
    return [
        {"key": key, "label": label, "default": DEFAULTS.get(key, [])}
        for key, (label, _) in FILTERS.items()
    ]


METRIC_INPUTS = {
    **{key: (key,) for key in BASE | EXTRA},
    "average_check": ("revenue", "checks"),
    "markup": ("revenue", "cost"),
    "gross_profit": ("revenue", "cost"),
    "discount_percent": ("discount", "gross_revenue"),
    "cost_share": ("revenue", "cost"),
    "guests_per_check": ("guests", "checks"),
}


def request_body(query, ids, fields, start, end):
    filters = {
        FILTERS[key][1]: {"filterType": "IncludeValues", "values": values}
        for key, values in query.effective_filters().items()
    }
    filters["Department.Id"] = {"filterType": "IncludeValues", "values": sorted(ids)}
    filters["OpenDate.Typed"] = {
        "filterType": "DateRange",
        "from": f"{start}T00:00:00",
        "to": f"{end + timedelta(days=1)}T00:00:00",
        "includeLow": True,
        "includeHigh": False,
    }
    return {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": [],
        "groupByColFields": [],
        "aggregateFields": fields,
        "filters": filters,
    }


def collect_reports(settings, bodies):
    """One licensed session, sequential queries and confirmed logout on every exit."""
    with (
        psycopg.connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            application_name="chaika-indicators",
        ) as db,
        reference_lock(db),
    ):
        try:
            response = httpx.post(
                "http://127.0.0.1:8010/api/v1/iiko/connections/primary/logout",
                timeout=10,
                trust_env=False,
            )
            response.raise_for_status()
            if response.json().get("state") != "logged_out":
                raise IikoError("iiko_session_unknown", "Выход из iiko не подтверждён.")
        except httpx.ConnectError:
            pass
        with TemporaryDirectory(prefix="chaika-indicators-") as directory:

            async def collect():
                client = IikoClient(settings)
                auth = IikoAuthService(settings, client)
                try:
                    results = []
                    for index, body in enumerate(bodies):
                        path = Path(directory) / f"{index}.json"
                        await auth.download_daily_sales(path, query=FixedReport(body))
                        payload = json.loads(path.read_bytes(), parse_float=Decimal)
                        if not isinstance(payload.get("data"), list):
                            raise ValueError("invalid_indicator_report")
                        results.append(payload["data"])
                    return results
                finally:
                    try:
                        try:
                            if (await auth.logout()).state != "logged_out":
                                raise ValueError("logout_not_confirmed")
                        except Exception as error:
                            raise IikoError(
                                "iiko_session_unknown",
                                "Выход из iiko не подтверждён.",
                                status_code=503,
                                outcome_unknown=True,
                            ) from error
                    finally:
                        await client.aclose()

            return asyncio.run(collect())


def parse_totals(rows, inputs):
    if len(rows) > 1:
        raise ValueError("Unexpected groups in aggregate report")
    result = {}
    mapping = BASE | EXTRA
    for key in inputs:
        value = rows[0].get(mapping[key]) if rows else None
        if value is None:
            result[key] = None if rows or key == "precheck_minutes" else Decimal(0)
        else:
            result[key] = Decimal(str(value))
            if not result[key].is_finite():
                raise ValueError("Invalid aggregate number")
    return result


def fetch_values(settings, query, ids, inputs):
    # Never average daily percentages or daily time averages: iiko aggregates the whole range.
    chunks = [inputs[i : i + 6] for i in range(0, len(inputs), 6)]
    periods = [(query.start, query.end), (query.previous_start, query.previous_end)]
    bodies = [
        request_body(query, ids, [(BASE | EXTRA)[k] for k in chunk], start, end)
        for start, end in periods
        for chunk in chunks
    ]
    reports = collect_reports(settings, bodies)
    values = []
    for index in range(2):
        total = {}
        for offset, chunk in enumerate(chunks):
            total.update(parse_totals(reports[index * len(chunks) + offset], chunk))
        values.append(calculate(total))
    return {"values": values, "observed_at": datetime.now(UTC)}


class IndicatorService:
    def __init__(self, settings, *, fetch=fetch_values, clock=monotonic):
        self.settings = settings
        self.fetch, self.clock = fetch, clock
        self.guard = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indicators")
        self.cache = OrderedDict()
        self.session_unknown = False

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _fetch(self, query, ids, inputs):
        if self.session_unknown:
            raise IikoError("iiko_session_unknown", "Выход из iiko не подтверждён.")
        try:
            return self.fetch(self.settings, query, ids, inputs), self.clock()
        except Exception as error:
            if error_code(error) == "iiko_session_unknown":
                self.session_unknown = True
            log.warning("Indicator unavailable: %s", error_code(error))
            raise

    def _job(self, scope, query, metric):
        if metric is not None and metric not in METRIC_INPUTS:
            raise HTTPException(422, "Неизвестный показатель.")
        ids = sorted(str(value) for value in scope.ids)
        if not ids:
            raise HTTPException(403, "Нет доступных ресторанов.")
        inputs = tuple(sorted(METRIC_INPUTS[metric] if metric else BASE | EXTRA))
        key = json.dumps(
            [
                ids,
                query.model_dump(mode="json", exclude={"day", "direct", "filters"})
                | {"filters": query.effective_filters()},
                inputs,
            ],
            sort_keys=True,
        )
        with self.guard:
            entry = self.cache.get(key)
            if entry and entry[0].done():
                future, failed_at = entry
                if future.exception() and failed_at is None:
                    failed_at = self.clock()
                    self.cache[key] = (future, failed_at)
                ttl = (
                    5
                    if future.exception()
                    and error_code(future.exception()) == "sync_already_running"
                    else 30
                )
                expired = (
                    (self.clock() - failed_at > ttl)
                    if future.exception()
                    else (self.clock() - future.result()[1] > 300)
                )
                if expired:
                    del self.cache[key]
                    entry = None
            if not entry:
                pending = sum(not f.done() for f, _ in self.cache.values())
                if pending >= 48:
                    raise HTTPException(429, "Очередь iiko занята. Повторите через минуту.")
                future = self.executor.submit(self._fetch, query, ids, inputs)
                self.cache[key] = entry = (future, None)
            self.cache.move_to_end(key)
            for old in list(self.cache):
                if len(self.cache) <= 256:
                    break
                if self.cache[old][0].done():
                    del self.cache[old]
            return entry[0]

    def get(self, scope, query, metric=None):
        future = self._job(scope, query, metric)
        if metric is None:
            # Preserve the old daily URL during a rolling frontend deployment.
            try:
                data, _ = future.result(timeout=240)
            except Exception as error:
                self._error(error)
        else:
            if not future.done():
                return {"status": "loading", "metric": metric, "retry_after": 2}
            try:
                data, _ = future.result()
            except Exception as error:
                if error_code(error) == "sync_already_running":
                    return {"status": "loading", "metric": metric, "retry_after": 5}
                self._error(error)
        periods = [(query.start, query.end), (query.previous_start, query.previous_end)]
        result = {
            "status": "ready",
            "source": "iiko_api",
            "metric": metric,
            "observed_at": data["observed_at"],
            "cache_seconds": 300,
        }
        for index, name in enumerate(("current", "previous")):
            start, end = periods[index]
            result[name] = {
                "start": start,
                "end": end,
                "date": end,
                "partial": end == datetime.now(ZONE).date(),
                "available": True,
                "observed_at": data["observed_at"],
                **(
                    {"value": data["values"][index][metric]}
                    if metric
                    else {"totals": data["values"][index]}
                ),
            }
        return result

    @staticmethod
    def _error(error):
        if error_code(error) == "iiko_session_unknown":
            raise HTTPException(
                503, "Соединение с iiko требует проверки. Фильтры доступны."
            ) from None
        raise HTTPException(503, "iiko пока не ответила. Повторите загрузку показателя.") from None


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
