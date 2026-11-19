"""Kitchen portions and net sales by accounting day, cooking place and local print hour."""

import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, DecimalException
from hashlib import sha256
from math import isfinite
from typing import Annotated
from uuid import UUID

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, BeforeValidator, Field, FiniteFloat

from app.integrations.iiko.errors import IikoError
from app.sync_references import SyncError
from app.sync_sales_history import error_code, request_for_day
from app.web.coverage import ZONE
from app.web.indicators import collect_reports
from app.web.warehouse_analytics import warehouse_ids

log = logging.getLogger(__name__)
MAX_DAYS = 7
DAY = "OpenDate.Typed"
DEPARTMENT = "Department.Id"
STORE = "Store.Id"
ORDER = "UniqOrderId.Id"
ITEM = "ItemSaleEvent.Id"
STATION_ID = "CookingPlace.Id"
STATION_NAME = "CookingPlace"
PRINTED = "DishServicePrintTime"
NET_REVENUE = "DishDiscountSumInt"
MAX_SAFE_INTEGER = (1 << 53) - 1
MAX_SAFE_REVENUE = Decimal(MAX_SAFE_INTEGER).scaleb(-2)
PORTIONS = "DishAmountInt"
STORNED = "Storned"
DELETION_FIELDS = ("OrderDeleted", "DeletedWithWriteoff")
LOCAL_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")


def parse_accounting_date(value):
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError("Expected YYYY-MM-DD")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError("Expected YYYY-MM-DD")
    return parsed


AccountingDate = Annotated[date, BeforeValidator(parse_accounting_date)]
PortionCount = Annotated[FiniteFloat, Field(ge=0)]
Kopecks = Annotated[int, Field(ge=-MAX_SAFE_INTEGER, le=MAX_SAFE_INTEGER)]


class KitchenStation(BaseModel):
    id: str
    name: str
    hours: list[PortionCount] = Field(min_length=24, max_length=24)
    revenue_kopecks: list[Kopecks] = Field(min_length=24, max_length=24)
    first_print_minutes: dict[date, int]


class SentToKitchen(BaseModel):
    stations: list[KitchenStation]
    unassigned_portions: PortionCount
    unprinted_portions: PortionCount
    unassigned_revenue_kopecks: Kopecks
    revenue_complete: bool
    has_active_print: bool


class KitchenHeatmap(BaseModel):
    department_id: UUID
    date_from: date
    date_to: date
    sent_to_kitchen: SentToKitchen


def heatmap_enabled(settings):
    # Raw transport connects to the primary Chain and its local collector. The
    # SaaS runtime needs a tenant-specific integration before mounting this feature.
    return settings.iiko_configured and os.environ.get("RESTCONTROL_RUNTIME_MODE") != "tenant"


def verify_primary(db, settings):
    row = db.execute(
        "SELECT fingerprint FROM chaika.sources WHERE id='primary' AND configured"
    ).fetchone()
    identity = f"{str(settings.iiko_base_url).rstrip('/')}\n{settings.iiko_login}"
    if row is None or row[0] != sha256(identity.encode()).hexdigest():
        raise SyncError("kitchen_heatmap_source_mismatch")


def include(values):
    return {"filterType": "IncludeValues", "values": list(values)}


def daily_request(department_id, stores, day):
    filters = {DEPARTMENT: include([str(department_id)]), STORNED: include(["FALSE"])}
    if stores is not None:
        filters[STORE] = include(stores)
    fields = [DAY, DEPARTMENT, STORE, ORDER, ITEM, STATION_ID, STATION_NAME, PRINTED, STORNED]
    return request_for_day(
        {
            "reportType": "SALES",
            "buildSummary": False,
            "groupByRowFields": [*fields, *DELETION_FIELDS],
            "groupByColFields": [],
            "aggregateFields": [PORTIONS, NET_REVENUE],
            "filters": filters,
        },
        day,
    ).iiko_body()


def nonzero_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except ValueError:
        return None
    return parsed if parsed.int else None


def finite_decimal(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("invalid_sales_number")
    try:
        number = Decimal(str(value))
    except DecimalException:
        raise ValueError("invalid_sales_number") from None
    if not number.is_finite() or not isfinite(float(number)):
        raise ValueError("invalid_sales_number")
    return number


def local_instant(printed):
    if not isinstance(printed, str) or not LOCAL_DATETIME.match(printed):
        return None
    try:
        stamp = datetime.fromisoformat(printed)
        if stamp.date() in (date.min, date(1970, 1, 1), date.max):
            return None
        if stamp.tzinfo is not None:
            return stamp.astimezone(UTC)
        # Offset-free stamps are local wall times. An ambiguous or nonexistent
        # local time cannot establish the hour of a kitchen event.
        first = stamp.replace(tzinfo=ZONE, fold=0)
        last = stamp.replace(tzinfo=ZONE, fold=1)
        if first.utcoffset() != last.utcoffset():
            return None
        return first.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def revenue_kopecks(value):
    try:
        amount = finite_decimal(value)
        if amount.copy_abs() > MAX_SAFE_REVENUE:
            return None
        rounded = amount.quantize(Decimal("0.01"))
        return int(rounded.scaleb(2)) if amount == rounded else None
    except (ValueError, DecimalException):
        return None


def station_label(row):
    station_id = row.get(STATION_ID)
    if not isinstance(station_id, str):
        return None
    station_id = station_id.strip()
    if station_id.casefold() in {"", "-", "null", "none", "unknown"}:
        return None
    try:
        parsed = UUID(station_id)
    except ValueError:
        pass  # The catalog declares ID_STRING, so IDs may be opaque strings.
    else:
        if not parsed.int:
            return None
        station_id = str(parsed)
    name = row.get(STATION_NAME)
    name = name.strip() if isinstance(name, str) else ""
    return station_id, name


class StationTotals:
    def __init__(self, station_id):
        self.id = station_id
        self.name = ""
        self.hours = [Decimal(0)] * 24
        self.revenue = [0] * 24
        self.first_print_minutes = {}

    def add(self, name, printed, business_date, quantity, kopecks):
        if name:
            self.name = name
        self.hours[printed.hour] += quantity
        self.revenue[printed.hour] += kopecks
        minutes = (
            (printed.date() - business_date).days * 1440 + printed.hour * 60 + printed.minute
        )
        self.first_print_minutes[business_date] = min(
            minutes, self.first_print_minutes.get(business_date, minutes)
        )


class HeatmapTotals:
    def __init__(self, department_id, stores, *, now):
        self.department_id = department_id
        self.now = now
        self.stores = None if stores is None else {UUID(store) for store in stores}
        self.stations = {}
        self.unassigned = Decimal(0)
        self.unassigned_revenue = 0
        self.revenue_complete = True
        self.has_active_print = False
        self.unprinted = Decimal(0)
        self.days = set()

    def consume(self, rows, body):
        day = body["filters"][DAY]["from"][:10]
        if day in self.days or not isinstance(rows, list):
            raise ValueError("kitchen_heatmap_invalid_daily_report")
        report_date = date.fromisoformat(day)
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or nonzero_uuid(row.get(DEPARTMENT)) != self.department_id:
                raise ValueError("kitchen_heatmap_department_mismatch")
            if self.stores is not None and nonzero_uuid(row.get(STORE)) not in self.stores:
                raise ValueError("kitchen_heatmap_store_mismatch")
            business_date = row.get(DAY)
            if business_date not in (day, f"{day}T00:00:00", f"{day}T00:00:00.000"):
                raise ValueError("kitchen_heatmap_day_mismatch")
            storned = row.get(STORNED)
            if storned == "TRUE" or storned is True:
                continue
            if storned != "FALSE" and storned is not False:
                raise ValueError("kitchen_heatmap_invalid_storno_status")
            quantity = finite_decimal(row.get(PORTIONS))
            if quantity <= 0:
                continue

            # The same item may have distinct OLAP groups, such as separate
            # stations. Reject a repeat of the full dimensional key.
            key = tuple(row.get(field) for field in body["groupByRowFields"])
            if any(value is not None and not isinstance(value, (str, bool)) for value in key):
                raise ValueError("kitchen_heatmap_invalid_group")
            if key in seen:
                raise ValueError("kitchen_heatmap_repeated_group")
            seen.add(key)

            deletion_states = [row.get(field) for field in DELETION_FIELDS]
            if any(not isinstance(state, str) or not state for state in deletion_states):
                raise ValueError("kitchen_heatmap_invalid_deletion_status")
            active = all(state == "NOT_DELETED" for state in deletion_states)
            instant = local_instant(row.get(PRINTED))
            if instant is None or instant > self.now:
                if active:
                    self.unprinted += quantity
                continue

            if active:
                self.has_active_print = True
            kopecks = revenue_kopecks(row.get(NET_REVENUE)) if active else 0
            if kopecks is None:
                self.revenue_complete = False
                kopecks = 0
            station = station_label(row)
            if station is None:
                self.unassigned += quantity
                self.unassigned_revenue += kopecks
            else:
                station_id, name = station
                if station_id not in self.stations:
                    self.stations[station_id] = StationTotals(station_id)
                self.stations[station_id].add(
                    name, instant.astimezone(ZONE), report_date, quantity, kopecks
                )
        self.days.add(day)

    def response(self, start, end):
        def safe_kopecks(value):
            nonlocal complete
            if abs(value) <= MAX_SAFE_INTEGER:
                return value
            complete = False
            return 0

        complete = self.revenue_complete
        stations = sorted(
            (
                KitchenStation(
                    id=station.id,
                    name=station.name or station.id,
                    hours=station.hours,
                    revenue_kopecks=[safe_kopecks(value) for value in station.revenue],
                    first_print_minutes=station.first_print_minutes,
                )
                for station in self.stations.values()
            ),
            key=lambda station: (station.name.casefold(), station.id),
        )
        unassigned_revenue = safe_kopecks(self.unassigned_revenue)
        return KitchenHeatmap(
            department_id=self.department_id,
            date_from=start,
            date_to=end,
            sent_to_kitchen=SentToKitchen(
                stations=stations,
                unassigned_portions=self.unassigned,
                unprinted_portions=self.unprinted,
                unassigned_revenue_kopecks=unassigned_revenue,
                revenue_complete=complete,
                has_active_print=self.has_active_print,
            ),
        )


def kitchen_heatmap(settings, scope, department_id, start, end, *, fetch=None):
    if not heatmap_enabled(settings):
        raise HTTPException(404, "Карта кухни недоступна.")
    if tuple(scope.ids) != (department_id,):
        raise HTTPException(403, "Нет доступа к выбранному ресторану.")
    now = datetime.now(UTC)
    if not start <= end <= now.astimezone(ZONE).date() or (end - start).days >= MAX_DAYS:
        raise HTTPException(422, f"Выберите период от 1 до {MAX_DAYS} учётных дней.")
    stores = warehouse_ids(scope)
    totals = HeatmapTotals(department_id, stores, now=now)
    bodies = [
        daily_request(department_id, stores, start + timedelta(days=offset))
        for offset in range((end - start).days + 1)
    ]
    fetch = collect_reports if fetch is None else fetch
    try:
        fetch(settings, bodies, validate_source=verify_primary, consume=totals.consume)
        if totals.days != {body["filters"][DAY]["from"][:10] for body in bodies}:
            raise ValueError("kitchen_heatmap_incomplete_report")
        return totals.response(start, end)
    except (IikoError, SyncError, httpx.HTTPError, OSError, ValueError, DecimalException) as error:
        log.warning("Kitchen heatmap unavailable: %s", error_code(error))
        raise HTTPException(
            503, "Данные кухни iiko временно недоступны. Повторите запрос позже."
        ) from None
