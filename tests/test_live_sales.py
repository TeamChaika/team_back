from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from decimal import Decimal
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from psycopg.rows import dict_row
from test_overview import HIDDEN, A, scope, seed_day
from test_reference_sync import db as db

from app.core.config import Settings
from app.web.coverage import ZONE
from app.web.live_sales import LiveSales, report_rows
from app.web.overview import read_overview


def bundle(day=None):
    day = day or datetime.now(ZONE).date().isoformat()
    rows = [
        {
            "Department.Id": str(A),
            "DishDiscountSumInt": "100.1",
            "DishSumInt": "150",
            "ProductCostBase.ProductCost": "40",
            "UniqOrderId": "2",
            "GuestNum": "3",
            "DishAmountInt": "2",
            "DishId": str(UUID(int=9)),
            "DishName": "Блюдо",
        },
        {"Department.Id": str(HIDDEN), "DishDiscountSumInt": "9999"},
    ]
    return {
        "id": uuid4(),
        "manifest": {"business_date": day},
        "checks": [],
        "reports": {
            kind: {
                "rows": rows,
                "info": {
                    "received_at": day + "T12:00:00+03:00",
                    "request": {
                        "groupByRowFields": (
                            ["Department.Id", "DishId", "DishName"]
                            if kind == "dishes"
                            else ["Department.Id"]
                        )
                    },
                },
            }
            for kind in ("daily", "dishes", "discounts")
        },
    }


def test_five_minute_cache_shared_by_parallel_requests():
    calls, now, entered, release = [], [0.0], Event(), Event()

    def fetch(_):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return bundle()

    cache = LiveSales(Settings(_env_file=None), fetch=fetch, clock=lambda: now[0])
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(cache.get) for _ in range(6)]
        assert entered.wait(2)
        release.set()
        results = [f.result() for f in futures]
    assert len(calls) == 1
    assert all(r[0] is results[0][0] for r in results)
    assert results[0][1]["cache_seconds"] == 300
    now[0] = 299
    cache.get()
    assert len(calls) == 1
    now[0] = 300
    cache.get()
    assert len(calls) == 2


def test_failure_is_marked_stale_bounded_and_never_uses_yesterday():
    now = [0.0]
    cache = LiveSales(
        SimpleNamespace(live_sales_cache_seconds=300),
        fetch=lambda _: bundle(),
        clock=lambda: now[0],
    )
    cache.get()

    def fail(_):
        raise ValueError("private credentials")

    cache.fetch = fail
    now[0] = 300
    assert cache.get()[1]["stale"] is True
    now[0] = 320
    assert cache.get()[1]["stale"] is True
    now[0] = 1200
    with pytest.raises(HTTPException) as error:
        cache.get()
    assert error.value.status_code == 503 and "private" not in error.value.detail
    cache.bundle = bundle("2023-03-01")
    now[0] = 321
    with pytest.raises(HTTPException):
        cache.get()


def test_live_rows_preserve_decimal_and_apply_restaurant_scope():
    data = bundle()
    rows = report_rows(data, "daily", scope(A))
    assert len(rows) == 1 and rows[0]["revenue"] == Decimal("100.1")
    assert rows[0]["live"] is True
    assert report_rows(data, "daily", scope()) == []
    assert report_rows(data, "dishes", scope(A), dish_id=str(UUID(int=10))) == []


def test_overview_replaces_saved_today_merges_history_and_scopes_top_dishes(db):
    seed_day(
        db,
        "2026-09-14",
        [{"revenue": 20}],
        dishes=[
            {"revenue": 20, "dimensions": {"DishId": str(UUID(int=9)), "DishName": "Old name"}}
        ],
    )
    seed_day(db, "2026-09-15", [{"revenue": 88888}])
    db.row_factory = dict_row
    result = read_overview(
        db,
        scope(A),
        datetime(2026, 9, 14).date(),
        datetime(2026, 9, 15).date(),
        "day",
        live_bundle=bundle("2026-09-15"),
    )
    assert result["current"]["totals"]["revenue"] == Decimal("120.1")
    assert result["top_dishes"][0]["revenue"] == Decimal("120.1")
    assert result["top_dishes"][0]["dish_name"] == "Блюдо"
    assert len(result["restaurants"]) == 1
    assert result["current"]["partial_days"][0]["date"].isoformat() == "2026-09-15"
