"""Persist all OLAP filter values independently of website reads and selected dates."""

import argparse
from datetime import date, datetime, timedelta
from uuid import UUID

import psycopg

from app.core.config import Settings
from app.sync_references import SyncError, reference_lock
from app.sync_scope import load_scope
from app.tenancy.connection import tenant_connect
from app.tenancy.sql import ANALYTICS_SCHEMA
from app.web.coverage import ZONE
from app.web.indicators import FILTERS, collect_reports

HISTORY_START = date(2023, 3, 1)


def filter_request(field, start, end):
    return {
        "reportType": "SALES",
        "buildSummary": False,
        "groupByRowFields": ["Department.Id", FILTERS[field][1]],
        "groupByColFields": [],
        "aggregateFields": ["UniqOrderId"],
        "filters": {
            "OpenDate.Typed": {
                "filterType": "DateRange",
                "from": f"{start}T00:00:00",
                "to": f"{end + timedelta(days=1)}T00:00:00",
                "includeLow": True,
                "includeHigh": False,
            }
        },
    }


def publish_filter(db, field, start, end, rows, *, replace, expected_scope=None):
    with reference_lock(db):
        return _publish_filter_locked(
            db, field, start, end, rows, replace=replace, expected_scope=expected_scope
        )


def _publish_filter_locked(db, field, start, end, rows, *, replace, expected_scope=None):
    scope = load_scope(db)
    if expected_scope is not None and scope.metadata() != expected_scope.metadata():
        raise SyncError("sync_scope_changed")
    values = set()
    for row in rows:
        value, department = row.get(FILTERS[field][1]), row.get("Department.Id")
        if value is None or department is None or not scope.department(department):
            continue
        value = str(value) if not isinstance(value, bool) else str(value).upper()
        if not 1 <= len(value) <= 500:
            raise ValueError("Invalid filter value")
        values.add(("primary", field, UUID(str(department)), value))
    # A failed capture never deletes a previously complete dictionary.
    with db.transaction():
        if replace:
            db.execute(
                (
                    f"DELETE FROM {ANALYTICS_SCHEMA}.indicator_filter_values WHERE "
                    f"source_id='primary' AND field=%s "
                    "AND (%s OR department_id=ANY(%s::uuid[]))"
                ),
                (field, scope.departments is None, list(scope.departments or [])),
            )
        with db.cursor() as cursor:
            cursor.executemany(
                f"INSERT INTO {ANALYTICS_SCHEMA}.indicator_filter_values VALUES(%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING",
                sorted(values, key=str),
            )
        if scope.departments is None:
            db.execute(
                f"INSERT INTO {ANALYTICS_SCHEMA}.indicator_filter_sync"
                "(source_id,field,period_start,period_end,value_count) "
                f"SELECT 'primary',%s,%s,%s,count(*) FROM "
                f"{ANALYTICS_SCHEMA}.indicator_filter_values "
                "WHERE source_id='primary' AND field=%s "
                "ON CONFLICT(source_id,field) DO UPDATE SET "
                "period_start=LEAST(indicator_filter_sync.period_start,excluded.period_start),"
                "period_end=excluded.period_end,synced_at=now(),value_count=excluded.value_count",
                (field, start, end, field),
            )
    return len(values)


def synchronize_filters(settings, stop, *, full=False, end=None, collect=collect_reports):
    end = end or datetime.now(ZONE).date()
    settings = settings.model_copy(
        update={
            "iiko_olap_sales_timeout_seconds": 120,
            "iiko_olap_sales_max_response_bytes": 16 * 1024 * 1024,
        }
    )
    for field in FILTERS:
        if stop.is_set():
            raise SyncError("filter_sync_interrupted")
        with tenant_connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            connector=psycopg.connect,
        ) as db:
            sync_scope = load_scope(db)
            if sync_scope.empty:
                return
            previous = db.execute(
                f"SELECT period_start,period_end FROM {ANALYTICS_SCHEMA}.indicator_filter_sync "
                "WHERE source_id='primary' AND field=%s",
                (field,),
            ).fetchone()
        start = (
            HISTORY_START
            if full or not previous
            else max(HISTORY_START, previous[1] - timedelta(days=7))
        )
        request = filter_request(field, start, end)
        if sync_scope.departments is not None:
            request["filters"]["Department.Id"] = {
                "filterType": "IncludeValues",
                "values": sorted(sync_scope.departments),
            }
        rows = collect(settings, [request])[0]
        with tenant_connect(
            settings.database_url.get_secret_value(),
            autocommit=True,
            connect_timeout=10,
            connector=psycopg.connect,
        ) as db:
            count = publish_filter(
                db, field, start, end, rows, replace=full or not previous, expected_scope=sync_scope
            )
        print(f"indicator_filter {field}: {count}", flush=True)


def read_filters(db, scope):
    if getattr(scope, "warehouse_restricted", False):
        # Existing dictionaries are grouped only by venue, so none of their values
        # can be proven to belong to an authorized warehouse.
        return {
            "options": {key: [] for key in FILTERS},
            "sync": {},
            "unavailable_reason": "Варианты фильтров по выбранным складам ещё не собраны.",
        }
    rows = db.execute(
        f"SELECT field,value FROM {ANALYTICS_SCHEMA}.indicator_filter_values WHERE "
        f"source_id='primary' "
        "AND department_id=ANY(%s::uuid[]) GROUP BY field,value ORDER BY field,lower(value),value",
        (scope.ids,),
    ).fetchall()
    status = db.execute(
        f"SELECT field,synced_at,period_start,period_end FROM "
        f"{ANALYTICS_SCHEMA}.indicator_filter_sync "
        "WHERE source_id='primary'"
    ).fetchall()
    options = {key: [] for key in FILTERS}
    for row in rows:
        if row["field"] in options:
            options[row["field"]].append(row["value"])
    return {
        "options": options,
        "sync": {r["field"]: {k: v for k, v in r.items() if k != "field"} for r in status},
    }


if __name__ == "__main__":
    from threading import Event

    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    synchronize_filters(Settings(), Event(), full=args.full)
