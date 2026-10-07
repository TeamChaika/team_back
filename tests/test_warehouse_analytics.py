"""Warehouse authorization is applied before querying or reusing analytics."""

from datetime import date
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services.sales_drilldown import query_for
from app.sync_indicator_filters import read_filters
from app.web.indicators import (
    IndicatorQuery,
    IndicatorService,
    fetch_values,
    fetch_warehouse_options,
    request_body,
)
from app.web.live_sales import report_rows
from app.web.overview import read_overview
from app.web.purchase_impact import read_purchase_impact
from app.web.purchase_impact_summary import add_weekly_impacts

DAY = date(2026, 9, 13)
STORE = str(UUID(int=10))
OTHER = str(UUID(int=11))


def scope(stores=(STORE,)):
    return SimpleNamespace(ids=[UUID(int=1)], store_ids=stores, warehouse_restricted=True)


def test_server_store_filter_cannot_be_removed_by_client():
    for filters in [{"Store.Id": [OTHER]}, {"warehouse": [OTHER]}]:
        with pytest.raises(ValidationError):
            IndicatorQuery(day=DAY, filters=filters)
    query = IndicatorQuery(day=DAY, filters={"dish_deleted": []})
    body = request_body(
        query, ["department"], ["UniqOrderId", "GuestNum"], DAY, DAY, stores=[STORE]
    )
    assert body["filters"]["Store.Id"] == {"filterType": "IncludeValues", "values": [STORE]}
    assert body["groupByRowFields"] == []
    with pytest.raises(HTTPException):
        request_body(query, ["department"], [], DAY, DAY, stores=[])


def test_every_kpi_capture_uses_union_filter_without_store_grouping(monkeypatch):
    from app.web import indicators

    bodies = []

    def collect(_, requests):
        bodies.extend(requests)
        return [[] for _ in requests]

    monkeypatch.setattr(indicators, "collect_reports", collect)
    fetch_values(
        None, IndicatorQuery(day=DAY), ["department"], ["checks", "guests"], stores=[STORE, OTHER]
    )
    assert len(bodies) == 2
    assert all(b["filters"]["Store.Id"]["values"] == [STORE, OTHER] for b in bodies)
    assert all(b["groupByRowFields"] == [] for b in bodies)


def test_cache_reuses_exact_warehouse_scope_and_isolates_changed_grants():
    seen = []

    def fetch(_, query, ids, inputs, *, stores):
        seen.append(stores)
        return {"values": [{}, {}], "observed_at": None}

    service = IndicatorService(None, fetch=fetch)
    try:
        query = IndicatorQuery(day=DAY)
        service._job(scope(), query, "revenue").result(2)
        service._job(scope(), query, "revenue").result(2)
        service._job(scope([OTHER]), query, "revenue").result(2)
        assert seen == [(STORE,), (OTHER,)]
        with pytest.raises(HTTPException):
            service._job(scope([]), query, "revenue")
        assert len(seen) == 2
    finally:
        service.close()


def test_options_never_return_foreign_store_values(monkeypatch):
    from app.web import indicators

    query = IndicatorQuery(day=DAY)
    bodies = []

    def collect(_, requests):
        bodies.extend(requests)
        return [[{"Store.Id": OTHER, "DishName": "private"}]]

    monkeypatch.setattr(indicators, "collect_reports", collect)
    with pytest.raises(ValueError):
        fetch_warehouse_options(None, query, ["department"], [STORE], "product")
    assert bodies[0]["filters"]["Store.Id"]["values"] == [STORE]
    assert bodies[0]["groupByRowFields"] == ["Store.Id", "DishName"]


def test_department_only_sources_never_query_or_return_data():
    class NoReads:
        def execute(self, *_):
            raise AssertionError("Unauthorized source was queried")

    db = NoReads()
    restricted = scope()
    assert not any(read_filters(db, restricted)["options"].values())
    for call in [
        lambda: report_rows({}, "daily", restricted),
        lambda: read_overview(db, restricted, DAY, DAY, "day"),
        lambda: read_purchase_impact(db, restricted, {}, (None, None, None, None)),
        lambda: add_weekly_impacts(db, restricted, {}),
    ]:
        with pytest.raises(HTTPException):
            call()


def test_discount_detail_query_preserves_mandatory_store_filter():
    context = {
        "request": {"filters": {}, "groupByRowFields": [], "aggregateFields": []},
        "business_date": DAY,
        "department_id": UUID(int=1),
        "discount_name": "discount",
        "allowed_store_ids": [STORE],
    }
    for order in [None, UUID(int=3)]:
        body = query_for(context, order)
        assert body["filters"]["Store.Id"]["values"] == [STORE]


def test_options_pending_failure_and_changed_grants_never_fallback(monkeypatch):
    from threading import Event

    from app.web import indicators

    entered, release = Event(), Event()
    seen = []

    def fail(_, query, ids, stores, field):
        seen.append(stores)
        entered.set()
        release.wait(3)
        raise ValueError("private upstream result")

    monkeypatch.setattr(indicators, "fetch_warehouse_options", fail)
    service = IndicatorService(None)
    try:
        query = IndicatorQuery(day=DAY)
        first = service.warehouse_options(scope(), query, "product")
        assert first["status"] == "loading" and first["values"] == []
        assert entered.wait(2)
        release.set()
        list(service.cache.values())[0][0].exception(timeout=2)
        result = service.warehouse_options(scope(), query, "product")
        assert result == {"values": [], "sync": None, "status": "unavailable"}
        service.warehouse_options(scope([OTHER]), query, "product")
        list(service.cache.values())[-1][0].exception(timeout=2)
        assert seen == [(STORE,), (OTHER,)]
        with pytest.raises(HTTPException):
            service.warehouse_options(scope([]), query, "product")
    finally:
        release.set()
        service.close()
