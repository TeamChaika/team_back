"""Bounded tenant-aware live snapshots; no global settings or analytics database."""

import hashlib
import json
import time
from collections import OrderedDict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from threading import BoundedSemaphore, Lock
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.web.overview import dates

from . import dashboard_reports as reports
from .dashboard_transport import fetch, target
from .repository import Problem

ZONE = ZoneInfo("Europe/Simferopol")
CACHE_SECONDS = 300


class DashboardService:
    def __init__(self, fetcher=fetch, clock=time.monotonic, now=lambda: datetime.now(UTC)):
        self.fetch = fetcher
        self.clock = clock
        self.now = now
        self.cache = OrderedDict()
        self.failures = OrderedDict()
        self.cache_size = 0
        self.lock = Lock()
        self.source_locks = [Lock() for _ in range(32)]
        self.slots = BoundedSemaphore(4)

    def _read(self, key):
        with self.lock:
            item = self.cache.get(key)
            if item and item[0] > self.clock():
                self.cache.move_to_end(key)
                value = json.loads(item[1])
                if "cached_error" in value:
                    raise Problem(503, value["cached_error"], value["message"])
                return value
            if item:
                self.cache_size -= len(item[1])
                del self.cache[key]
        return None

    def _put(self, key, value, ttl=CACHE_SECONDS):
        body = json.dumps(value, default=str, ensure_ascii=False).encode()
        if len(body) > 16 * 1024 * 1024:
            raise Problem(413, "report_too_large", "Выберите меньший период или один ресторан")
        with self.lock:
            old = self.cache.pop(key, None)
            if old:
                self.cache_size -= len(old[1])
            self.cache[key] = (self.clock() + ttl, body)
            self.cache_size += len(body)
            while len(self.cache) > 64 or self.cache_size > 32 * 1024 * 1024:
                _, removed = self.cache.popitem(last=False)
                self.cache_size -= len(removed[1])
        return json.loads(body)

    def _cached(self, source, suffix, load):
        key = (*source.cache_key, *suffix)
        existing = self._read(key)
        if existing is not None:
            return existing
        # Canonical host serializes even two companies sharing one iiko endpoint.
        index = int(hashlib.sha256(target(source.url).encode()).hexdigest(), 16) % 32
        lock = self.source_locks[index]
        if not lock.acquire(timeout=2):
            raise Problem(503, "iiko_busy", "Данные компании загружаются. Повторите запрос")
        try:
            existing = self._read(key)
            if existing is not None:
                return existing
            with self.lock:
                failure = self.failures.get(source.cache_key)
                if failure and failure[0] > self.clock():
                    raise Problem(503, failure[1], failure[2])
                self.failures.pop(source.cache_key, None)
            if not self.slots.acquire(blocking=False):
                raise Problem(503, "iiko_busy", "Загрузка данных занята. Повторите запрос")
            try:
                with localcontext() as context:
                    context.prec = 70
                    return self._put(key, load())
            except Problem as error:
                if error.status == 503:
                    self._put(key, {"cached_error": error.code, "message": error.message}, ttl=30)
                    # Different period/filter keys must not create a login storm,
                    # especially when auth/logout timed out and the token is unknown.
                    cooldown = 300 if error.code in {"iiko_logout_failed", "iiko_timeout"} else 30
                    with self.lock:
                        self.failures[source.cache_key] = (
                            self.clock() + cooldown,
                            error.code,
                            error.message,
                        )
                        while len(self.failures) > 64:
                            self.failures.popitem(last=False)
                raise
            finally:
                self.slots.release()
        finally:
            lock.release()

    def status(self, observed):
        return {
            "source": "iiko_api",
            "status": "ready",
            "observed_at": observed,
            "expires_at": observed + timedelta(seconds=CACHE_SECONDS),
            "cache_seconds": CACHE_SECONDS,
            "timezone": "Europe/Simferopol",
        }

    def references(self, source):
        def load():
            bodies = self.fetch(source, [("departments", None), ("columns", None)])
            return {
                "departments": reports.departments(bodies[0]),
                "available": sorted(reports.capabilities(bodies[1])),
                "data_status": self.status(self.now()),
            }

        return self._cached(source, ("references-v1",), load)

    def meta(self, source):
        refs = self.references(source)
        return {
            "user": {
                "id": source.user_id,
                "display_name": source.user_name,
                "role": "company_admin",
                "all_departments": True,
                "password_change_required": False,
            },
            "company": {"id": source.company_id, "name": source.company_name},
            "departments": refs["departments"],
            "sales_dates": [],
            "balance_dates": [],
            "today": self.now().astimezone(ZONE).date(),
            "sections": ["overview", "sales"],
            "supported_sales_kinds": ["daily", "dishes"],
            "modules": ["iiko"],
            "can_manage": False,
            "documents_enabled": False,
            "live_sales_enabled": True,
            "data_status": refs["data_status"],
        }

    def _scope(self, source, selected):
        refs = self.references(source)
        known = {row["id"] for row in refs["departments"]}
        if set(selected) - known:
            raise Problem(403, "department_forbidden", "Заведение недоступно этой компании")
        ids = sorted(set(selected) or known)
        if len(ids) > 100:
            raise Problem(422, "too_many_departments", "Выберите не более 100 заведений")
        if not ids:
            raise Problem(503, "departments_empty", "iikoChain не вернул заведений для отчёта")
        return refs, ids

    def period(self, start, end, previous=False):
        today = self.now().astimezone(ZONE).date()
        if start < date(2000, 1, 1) or end < start or end > today or (end - start).days >= 31:
            raise Problem(422, "invalid_period", "Выберите период до 31 дня, не позднее сегодня")
        if previous and start - timedelta(days=(end - start).days + 1) < date(2000, 1, 1):
            raise Problem(422, "invalid_period", "Период сравнения выходит за допустимую дату")

    def overview(self, source, start, end, selected, grain):
        self.period(start, end, previous=True)
        refs, ids = self._scope(source, selected)

        def load():
            available = set(refs["available"])
            count = (end - start).days + 1
            before = start - timedelta(days=count)
            monetary = [key for key in ("revenue", "cost") if key in available]
            fields = [key for key in ("revenue", "cost", "checks", "guests") if key in available]
            dish_fields = [key for key in ("revenue", "quantity") if key in available]
            bodies = [
                reports.query(before, end, ids, ["OpenDate.Typed", "Department.Id"], monetary),
                reports.query(
                    before,
                    end,
                    ids,
                    ["OpenDate.Typed", "Department.Id", "DishId", "DishName"],
                    dish_fields,
                ),
                reports.query(start, end, ids, [], fields),
                reports.query(before, start - timedelta(days=1), ids, [], fields),
            ]
            raw = self.fetch(source, [("sales", body) for body in bodies])
            parsed = [
                reports.rows(body, request, before, end, ids)
                for body, request in zip(raw, bodies, strict=True)
            ]
            observed = self.now()
            result = reports.overview(
                SimpleNamespace(ids=ids, departments=refs["departments"]),
                start,
                end,
                grain,
                parsed[0],
                parsed[1],
                reports.aggregate(parsed[2], available, count),
                reports.aggregate(parsed[3], available, count),
                observed,
                available,
            )
            return {**result, "data_status": self.status(observed)}

        return self._cached(source, ("overview-v1", str(start), str(end), tuple(ids), grain), load)

    def sales(self, source, start, end, selected, kind, dish_id=None, dish_name=None):
        self.period(start, end)
        refs, ids = self._scope(source, selected)

        def load():
            available = set(refs["available"])
            # Daily row counters are not used as whole-period counters.
            groups = ["OpenDate.Typed", "Department.Id"]
            keys = ("revenue", "cost")
            if kind == "dishes":
                groups += ["DishId", "DishName"]
                keys = ("revenue", "cost", "quantity")
            fields = [key for key in keys if key in available]
            request = reports.query(start, end, ids, groups, fields, dish_id, dish_name)
            aggregate_keys = [
                key
                for key in ("revenue", "cost", "checks", "guests", "quantity")
                if key in available
            ]
            aggregate_request = reports.query(
                start, end, ids, [], aggregate_keys, dish_id, dish_name
            )
            bodies = self.fetch(source, [("sales", request), ("sales", aggregate_request)])
            raw = reports.rows(bodies[0], request, start, end, ids)
            aggregate_raw = reports.rows(bodies[1], aggregate_request, start, end, ids)
            observed = self.now()
            total = reports.aggregate(aggregate_raw, available, (end - start).days + 1)
            total.update(
                discount=None,
                return_sum=None,
                quantity=(
                    aggregate_raw[0]["quantity"]
                    if aggregate_raw
                    else Decimal(0)
                    if "quantity" in available
                    else None
                ),
            )
            # A dish selection must not imply checks/guests for the whole restaurant.
            if kind == "dishes":
                total.update(checks=None, guests=None, average_check=None, guests_per_day=None)
            return {
                "kind": kind,
                "rows": reports.sales_rows(
                    raw,
                    kind,
                    request,
                    {d["id"]: d["name"] for d in refs["departments"]},
                    observed,
                    str(uuid4()),
                ),
                "totals": total,
                "hourly": [],
                "payment_groups": [],
                "discount_groups": [],
                "loaded_dates": dates(start, end),
                "missing_dates": [],
                "complete": True,
                "start": start,
                "end": end,
                "reconciliation_issues": [],
                "partial_days": [{"date": end, "observed_at": observed}]
                if end == observed.astimezone(ZONE).date()
                else [],
                "data_status": self.status(observed),
            }

        return self._cached(
            source, ("sales-v1", kind, str(start), str(end), tuple(ids), dish_id, dish_name), load
        )
