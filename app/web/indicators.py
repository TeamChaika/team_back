"""Direct iiko aggregates with a durable last-good cache, never sums of stored sales."""

import asyncio
import json
import logging
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic
from typing import Annotated
from uuid import UUID

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
from app.web.sync_policy import SyncPolicy, guard_requests
from app.web.warehouse_analytics import warehouse_ids

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
    include_archived: bool = False
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


def request_body(query, ids, fields, start, end, *, stores=None):
    filters = {
        FILTERS[key][1]: {"filterType": "IncludeValues", "values": values}
        for key, values in query.effective_filters().items()
    }
    filters["Department.Id"] = {"filterType": "IncludeValues", "values": sorted(ids)}
    if stores is not None:
        if not stores:
            raise HTTPException(403, "Нет доступных складов.")
        # Authorization is injected after user filters and cannot be removed by them.
        filters["Store.Id"] = {"filterType": "IncludeValues", "values": sorted(stores)}
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
        guard_requests(db, bodies)
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


def fetch_values(settings, query, ids, inputs, *, stores=None):
    # Never average daily percentages or daily time averages: iiko aggregates the whole range.
    chunks = [inputs[i : i + 6] for i in range(0, len(inputs), 6)]
    periods = [(query.start, query.end), (query.previous_start, query.previous_end)]
    bodies = [
        request_body(query, ids, [(BASE | EXTRA)[k] for k in chunk], start, end, stores=stores)
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


def fetch_warehouse_options(settings, query, ids, stores, field):
    """Capture only options represented in authorized sale rows, by UUID."""
    body = request_body(query, ids, ["DishAmountInt"], query.start, query.end, stores=stores)
    dimension = FILTERS[field][1]
    body["filters"].pop(dimension, None)
    body["groupByRowFields"] = ["Store.Id", dimension]
    reports = collect_reports(settings, [body])
    if len(reports) != 1 or len(reports[0]) > 20000:
        raise ValueError("Invalid warehouse filter options")
    values = set()
    for row in reports[0]:
        if str(row.get("Store.Id")) not in stores:
            raise ValueError("Warehouse filter options scope mismatch")
        value = row.get(dimension)
        if value is not None:
            value = str(value) if not isinstance(value, bool) else str(value).upper()
            if not 1 <= len(value) <= 500:
                raise ValueError("Invalid warehouse filter value")
            values.add(value)
    return {"values": sorted(values), "sync": {"observed_at": datetime.now(UTC)}}


class IndicatorService:
    def __init__(
        self,
        settings,
        *,
        fetch=fetch_values,
        clock=monotonic,
        policy_reader=None,
        store=None,
        repository=None,
        utcnow=None,
    ):
        self.policy_reader = policy_reader or SyncPolicy
        self.settings = settings
        self.fetch, self.clock = fetch, clock
        self.guard = Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="indicators")
        self.cache = OrderedDict()
        self.session_unknown = False
        self.store, self.repository = store, repository
        self.utcnow = utcnow or (lambda: datetime.now(UTC))
        self.stop = Event()
        self.thread = None
        self.pending = {}
        self.last_warm = None

    def start(self):
        if self.store is not None and self.thread is None:
            self.thread = Thread(target=self._run, name="indicator-refresh", daemon=True)
            self.thread.start()

    def _run(self):
        while not self.stop.is_set():
            try:
                self.tick()
            except Exception as error:
                log.warning("Indicator background unavailable: %s", error_code(error))
            self.stop.wait(5)

    def close(self):
        self.stop.set()
        if self.thread is not None:
            self.thread.join()
        self.executor.shutdown(wait=True, cancel_futures=True)

    def _spec(self, scope, query):
        policy = self.policy_reader()
        ids = self._department_ids(scope, policy, query.include_archived)
        stores = warehouse_ids(scope)
        filters = query.effective_filters()
        filters |= {key: [] for key in DEFAULTS if key not in filters}
        spec = {
            "version": 1,
            "source": "primary",
            "policy_revision": policy.revision,
            "departments": ids,
            "stores": list(stores) if stores is not None else None,
            "query": query.model_dump(mode="json", exclude={"day", "direct", "filters"})
            | {"filters": filters},
        }
        key = sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        return key, spec

    def _register(self, scope, query):
        key, spec = self._spec(scope, query)
        if not self.store.register(key, spec, scope.user["id"], list(scope.selection_ids)):
            raise HTTPException(429, "Очередь показателей занята. Повторите через минуту.")
        return key

    def _authorized(self, row):
        from app.web.permissions import require_section

        scope = self.repository.scope(
            UUID(str(row["user_id"])), tuple(UUID(str(v)) for v in row["selection"])
        )
        require_section(scope.user, "indicators")
        query = IndicatorQuery(**row["spec"]["query"])
        key, _ = self._spec(scope, query)
        if key != row["key"]:
            raise HTTPException(403, "Область показателей изменилась.")
        return scope, query

    def _refresh(self, key):
        token = self.store.claim(key)
        if token is None:
            return
        try:
            row = self.store.read(key)
            scope, query = self._authorized(row)
            spec = row["spec"]
            data, _ = self._fetch(query, spec["departments"], tuple(BASE | EXTRA), spec["stores"])
            # Permissions/policy may have changed during the network request.
            self._authorized(row)
            self.store.success(key, token, data)
        except HTTPException as error:
            if error.status_code in (403, 423):
                self.store.discard(key, token)
            else:
                self.store.failure(key, token, error_code(error))
        except Exception as error:
            self.store.failure(key, token, error_code(error))

    def _schedule(self, key):
        with self.guard:
            self.pending = {k: f for k, f in self.pending.items() if not f.done()}
            if key not in self.pending and len(self.pending) < 48:
                self.pending[key] = self.executor.submit(self._refresh, key)
                self.pending[key].add_done_callback(self._refresh_done)

    @staticmethod
    def _refresh_done(future):
        if not future.cancelled() and future.exception() is not None:
            log.warning("Indicator refresh failed: %s", error_code(future.exception()))

    def tick(self):
        """One nonblocking background tick, also directly testable without HTTP traffic."""
        now = self.utcnow()
        if self.last_warm is None or (now - self.last_warm).total_seconds() >= 300:
            query = IndicatorQuery(day=now.astimezone(ZONE).date())
            seen = set()
            for scope in self.repository.indicator_warm_scopes():
                try:
                    key, _ = self._spec(scope, query)
                    if key not in seen:
                        self._register(scope, query)
                        seen.add(key)
                except HTTPException:
                    continue
                if len(seen) >= 32:
                    break
            self.last_warm = now
        for row in self.store.due():
            if self.stop.is_set():
                break
            self._schedule(row["key"])

    def _persistent_job(self, scope, query):
        key, _ = self._spec(scope, query)
        row = self.store.read(key)
        now = self.utcnow()
        # Hits only read; renew the demand lease at most once an hour.
        if row is None or row["expires_at"] - now < timedelta(hours=23):
            self._register(scope, query)
            row = self.store.read(key)
            now = self.utcnow()
        if row is None:
            # Another process may evict a newly admitted key at capacity. Retry by polling.
            return Future()
        if row["due_at"] <= now:
            self._schedule(key)
        future = Future()
        if row["payload"] is not None:
            data = dict(row["payload"])
            data["values"] = [
                {
                    key: Decimal(value) if isinstance(value, str) else value
                    for key, value in totals.items()
                }
                for totals in data["values"]
            ]
            data["cache"] = {
                "stale": (now - row["observed_at"]).total_seconds() >= 300,
                "refreshing": bool(row["lease_until"] and row["lease_until"] > now)
                or row["due_at"] <= now,
                "refresh_error": row["last_error"],
                "next_refresh_at": row["due_at"],
            }
            future.set_result((data, self.clock()))
        elif row["last_error"] and row["due_at"] > now:
            future.set_exception(IikoError(row["last_error"], "Показатели временно недоступны."))
        # Cold entries are polled, never held behind the provider request.
        return future

    def _department_ids(self, scope, policy, include_archived=False):
        """Narrow implicit all-network reads; explicit selections remain strict."""
        from app.web.sync_policy import analytics_department_ids

        ids = analytics_department_ids(scope, policy, include_archived)
        if getattr(scope, "selection_ids", None) == () and policy.departments is not None:
            active = [value for value in ids if value in policy.departments]
            if not active:
                policy.require(ids)
            ids = active
        policy.require(ids)
        if not ids:
            raise HTTPException(403, "Нет доступных ресторанов.")
        return ids

    def _fetch(self, query, ids, inputs, stores=None):
        if self.session_unknown:
            raise IikoError("iiko_session_unknown", "Выход из iiko не подтверждён.")
        try:
            options = {"stores": stores} if stores is not None else {}
            return self.fetch(self.settings, query, ids, inputs, **options), self.clock()
        except Exception as error:
            if error_code(error) == "iiko_session_unknown":
                self.session_unknown = True
            log.warning("Indicator unavailable: %s", error_code(error))
            raise

    def _fetch_options(self, query, ids, stores, field):
        if self.session_unknown:
            raise IikoError("iiko_session_unknown", "Выход из iiko не подтверждён.")
        try:
            return fetch_warehouse_options(self.settings, query, ids, stores, field), self.clock()
        except Exception as error:
            if error_code(error) == "iiko_session_unknown":
                self.session_unknown = True
            raise

    def warehouse_options(self, scope, query, field):
        """Bounded asynchronous cache; pending/error results never use venue options."""
        if field not in FILTERS:
            raise HTTPException(422, "Неизвестный фильтр.")
        policy = self.policy_reader()
        ids = self._department_ids(scope, policy, query.include_archived)
        stores = warehouse_ids(scope)
        if stores is None or not ids:
            raise HTTPException(403, "Нет доступных складов.")
        key = json.dumps(
            [
                "warehouse_options",
                policy.revision,
                ids,
                stores,
                field,
                query.model_dump(mode="json"),
            ],
            sort_keys=True,
        )
        with self.guard:
            entry = self.cache.get(key)
            if entry and entry[0].done():
                future, failed_at = entry
                if future.exception():
                    failed_at = failed_at if failed_at is not None else self.clock()
                    self.cache[key] = (future, failed_at)
                    expired = self.clock() - failed_at > 30
                else:
                    expired = self.clock() - future.result()[1] > 300
                if expired:
                    del self.cache[key]
                    entry = None
            if not entry:
                if sum(not future.done() for future, _ in self.cache.values()) >= 48:
                    raise HTTPException(429, "Очередь iiko занята. Повторите через минуту.")
                future = self.executor.submit(self._fetch_options, query, ids, stores, field)
                self.cache[key] = entry = (future, None)
            self.cache.move_to_end(key)
            for old in list(self.cache):
                if len(self.cache) <= 256:
                    break
                if self.cache[old][0].done():
                    del self.cache[old]
            future = entry[0]
        if not future.done():
            return {"values": [], "sync": None, "status": "loading", "retry_after": 2}
        try:
            result, _ = future.result()
            return {**result, "status": "ready"}
        except HTTPException:
            raise
        except Exception:
            return {"values": [], "sync": None, "status": "unavailable"}

    def _job(self, scope, query, metric):
        policy = self.policy_reader()
        ids = self._department_ids(scope, policy, query.include_archived)
        if metric is not None and metric not in METRIC_INPUTS:
            raise HTTPException(422, "Неизвестный показатель.")
        stores = warehouse_ids(scope)
        if self.store is not None:
            return self._persistent_job(scope, query)
        inputs = tuple(sorted(BASE | EXTRA))
        key = json.dumps(
            [
                policy.revision,
                ids,
                stores,
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
                future = self.executor.submit(self._fetch, query, ids, inputs, stores)
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
        if self.store is not None and not future.done():
            return {"status": "loading", "metric": metric, "retry_after": 2}
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
            **data.get("cache", {}),
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
        if isinstance(error, HTTPException):
            raise error
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
