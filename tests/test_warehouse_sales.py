"""Restricted historical SALES never reads venue snapshots or sums store check counts."""

import json
from collections import defaultdict
from contextlib import nullcontext
from copy import deepcopy
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import HTTPException
from test_sales_import import review as review

from app.web import warehouse_sales as sales
from app.web.repository import Repository, Scope

DEPT, OTHER = UUID(int=1), UUID(int=2)
A, B, HIDDEN = UUID(int=11), UUID(int=12), UUID(int=13)
DAY = date(2026, 9, 10)


def scope(*stores):
    return Scope(
        {"id": "user", "role": "manager", "warehouse_scope_mode": "selected"},
        ({"id": DEPT, "name": "Venue", "code": "test"},),
        None,
        stores,
        (),
    )


@pytest.fixture
def provider(monkeypatch, review):
    _, manifest = review
    templates = {key: info["request"] for key, info in manifest["reports"].items()}
    templates["discounts"]["aggregateFields"].append("DiscountSum")
    # Fixture source: one check uses two allowed stores. A third store and venue
    # have business data too, and must not contribute to any metric.
    items = [
        ("2026-09-09", DEPT, A, "old", "Soup", 50),
        ("2026-09-10", DEPT, A, "one", "Soup", 100),
        ("2026-09-10", DEPT, B, "one", "Tea", 200),
        ("2026-09-10", DEPT, HIDDEN, "hidden", "Cake", 9000),
        ("2026-09-10", OTHER, A, "other-venue", "Soup", 8000),
    ]
    calls = []

    def collect(settings, bodies):
        result = []
        for body in bodies:
            calls.append(deepcopy(body))
            assert "Store.Id" not in body["groupByRowFields"]
            stores = body["filters"]["Store.Id"]["values"]
            departments = body["filters"]["Department.Id"]["values"]
            period = body["filters"]["OpenDate.Typed"]
            groups = defaultdict(list)
            for day, dept, store, order, dish, revenue in items:
                if str(store) not in stores or str(dept) not in departments:
                    continue
                if not period["from"][:10] <= day < period["to"][:10]:
                    continue
                dimensions = {
                    "OpenDate.Typed": day,
                    "Department.Id": str(dept),
                    "Department": "Venue",
                    "DishId": dish,
                    "DishName": dish,
                    "PayTypes.Group": "Cash",
                    "PayTypes": "Cash",
                    "ItemSaleEventDiscountType": "Discount",
                    "Storned": "FALSE",
                    "OrderWaiter.Name": "Waiter",
                    "HourOpen": "12",
                }
                key = tuple((field, dimensions[field]) for field in body["groupByRowFields"])
                groups[key].append((order, revenue))
            raw = []
            for dimensions, values in groups.items():
                metrics = {
                    "DishDiscountSumInt": sum(value for _, value in values),
                    "ProductCostBase.ProductCost": sum(value for _, value in values) / Decimal(2),
                    "UniqOrderId": len({order for order, _ in values}),
                    "GuestNum": 2 * len({order for order, _ in values}),
                    "DiscountSum": sum(value for _, value in values) / Decimal(10),
                }
                raw.append(
                    dict(dimensions) | {key: metrics[key] for key in body["aggregateFields"]}
                )
            result.append(raw)
        return result

    monkeypatch.setattr(sales.psycopg, "connect", lambda *a, **k: nullcontext(object()))
    monkeypatch.setattr(sales, "approved_templates", lambda db: ("fixture", templates))
    monkeypatch.setattr(sales, "collect_reports", collect)
    settings = SimpleNamespace(database_url=SimpleNamespace(get_secret_value=lambda: "unused"))
    repo = Repository.__new__(Repository)
    repo.warehouse_sales = sales.WarehouseSales(settings)
    # Any accidental broad database history fallback fails the test immediately.
    repo.connection = lambda *a, **k: pytest.fail("venue snapshot read forbidden")
    return repo, calls, templates


def test_actual_iiko_metadata_identifies_store_uuid_filter():
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/warehouse_sales_columns.json").read_text()
    )
    assert fixture["columns"]["Store.Id"]["type"] == "ID_STRING"
    assert fixture["columns"]["Store.Id"]["filteringAllowed"] is True
    assert fixture["columns"]["Store.Name"]["name"] == "Со склада"


def test_history_union_unique_checks_and_cache_grants(provider):
    repo, calls, _ = provider
    result = repo.sales(scope(A, B), "daily", DAY, DAY)
    assert result["totals"]["revenue"] == "300"
    assert result["totals"]["checks"] == "1"  # same check spans stores A and B
    assert result["totals"]["guests"] == "2"
    assert result["complete"] is True
    assert result["loaded_dates"] == [str(DAY)]
    assert result["missing_dates"] == []
    assert len(calls) == 1
    assert repo.sales(scope(B, A), "daily", DAY, DAY) == result
    assert len(calls) == 1
    narrow = repo.sales(scope(A), "daily", DAY, DAY)
    assert narrow["totals"]["revenue"] == "100"
    assert len(calls) == 2
    assert calls[0]["filters"]["Store.Id"]["values"] == [str(A), str(B)]


@pytest.mark.parametrize("kind", sorted(sales.KINDS))
def test_all_seven_reports_use_same_authorized_history(provider, kind):
    repo, calls, _ = provider
    result = repo.sales(scope(A, B), kind, DAY, DAY)
    assert result["totals"]["revenue"] == "300"
    assert {row["department_id"] for row in result["rows"]} == {str(DEPT)}
    assert result["complete"] is True
    assert all(
        row["request"]["filters"]["Store.Id"]["values"] == [str(A), str(B)]
        for row in result["rows"]
    )
    if kind == "payments":
        assert result["payment_groups"][0]["totals"]["revenue"] == "300"
    if kind == "hours":
        assert result["hourly"][0]["checks"] == "1"


def test_dish_drilldown_keeps_scope_and_uses_cached_capture(provider):
    repo, calls, _ = provider
    result = repo.sales(scope(A, B), "dishes", DAY, DAY, dish_id="Soup")
    assert result["totals"]["revenue"] == "100"
    assert repo.sales(scope(A, B), "dishes", DAY, DAY, dish_id="Cake")["rows"] == []
    assert len(calls) == 1


def test_overview_previous_period_trends_and_ranking_are_scoped(provider):
    repo, calls, _ = provider
    result = repo.overview(scope(A, B), DAY, DAY, "day")
    assert result["current"]["totals"]["revenue"] == "300"
    assert result["current"]["totals"]["checks"] == "1"
    assert result["previous"]["totals"]["revenue"] == "50"
    assert result["changes"]["revenue"]["absolute"] == "250"
    assert [row["dish_name"] for row in result["top_dishes"]] == ["Tea", "Soup"]
    assert len(result["restaurants"]) == 1
    assert result["trend"][0]["current"]["totals"]["checks"] == "1"
    assert len(calls) == 2
    assert all(body["filters"]["OpenDate.Typed"]["from"] == "2026-09-09T00:00:00" for body in calls)


def test_existing_template_filters_cannot_override_permissions(provider):
    _, _, templates = provider
    template = deepcopy(templates["daily"])
    template["filters"]["Store.Id"] = {"filterType": "IncludeValues", "values": [str(HIDDEN)]}
    body = sales.scoped_request(template, scope(A), DAY, DAY)
    assert body["filters"]["Store.Id"]["values"] == [str(A)]
    assert template["filters"]["Store.Id"]["values"] == [str(HIDDEN)]
    with pytest.raises(HTTPException) as error:
        sales.scoped_request(template, scope(), DAY, DAY)
    assert error.value.status_code == 403


def test_expired_or_changed_grants_never_fall_back_on_provider_failure(provider):
    repo, _, _ = provider
    now = [0]
    repo.warehouse_sales.clock = lambda: now[0]
    repo.sales(scope(A, B), "daily", DAY, DAY)
    repo.warehouse_sales.fetch = lambda *a: (_ for _ in ()).throw(
        ValueError("private provider error")
    )
    with pytest.raises(HTTPException) as error:
        repo.sales(scope(A), "daily", DAY, DAY)
    assert error.value.status_code == 503 and "private" not in error.value.detail
    now[0] = 300
    with pytest.raises(HTTPException):
        repo.sales(scope(A, B), "daily", DAY, DAY)


@pytest.mark.parametrize("bad", ["department", "date", "nan", "duplicate", "missing"])
def test_invalid_provider_capture_is_unavailable(provider, monkeypatch, bad):
    repo, _, templates = provider
    raw = {
        "OpenDate.Typed": str(DAY),
        "Department.Id": str(DEPT),
        "Department": "Venue",
        "DishDiscountSumInt": 100,
        "ProductCostBase.ProductCost": 50,
        "UniqOrderId": 1,
        "GuestNum": 2,
    }
    if bad == "department":
        raw["Department.Id"] = str(OTHER)
    elif bad == "date":
        raw["OpenDate.Typed"] = "2026-09-11"
    elif bad == "nan":
        raw["DishDiscountSumInt"] = Decimal("NaN")
    elif bad == "missing":
        raw.pop("UniqOrderId")
    monkeypatch.setattr(
        sales, "collect_reports", lambda *a: [[raw, raw] if bad == "duplicate" else [raw]]
    )
    with pytest.raises(HTTPException) as error:
        repo.sales(scope(A), "daily", DAY, DAY)
    assert error.value.status_code == 503


def test_cached_capture_is_not_mutable_by_callers(provider):
    repo, _, _ = provider
    service = repo.warehouse_sales
    first = service.get(scope(A), DAY, DAY, ("daily",))
    first["rows"]["daily"][0]["revenue"] = Decimal(999999)
    assert service.get(scope(A), DAY, DAY, ("daily",))["rows"]["daily"][0]["revenue"] == 100


def test_discount_drilldown_orders_items_pagination_and_grant_revocation(provider, monkeypatch):
    from app.schemas.sales_drilldown import DiscountDetailsQuery

    repo, _, _ = provider
    report = repo.sales(scope(A, B), "discounts", DAY, DAY)
    row = report["rows"][0]
    assert row["drilldown_available"] is True
    query = DiscountDetailsQuery(report_id=row["report_id"], ordinal=row["ordinal"])
    requests = []

    def collect(settings, bodies):
        body = bodies[0]
        requests.append(body)
        assert body["filters"]["Store.Id"]["values"] == [str(A), str(B)]
        assert body["filters"]["Department.Id"]["values"] == [str(DEPT)]
        if "UniqOrderId.Id" in body["filters"]:
            return [
                [
                    {
                        "ItemSaleEvent.Id": str(UUID(int=200 + index)),
                        "DishId": str(UUID(int=100 + index)),
                        "DishName": "Dish",
                        "ItemSaleEventDiscountType": "Discount",
                        "DishDiscountSumInt": 1,
                        "DiscountSum": Decimal("0.1"),
                        "DishAmountInt": 1,
                    }
                    for index in range(2)
                ]
            ]
        return [
            [
                {
                    "OpenDate.Typed": str(DAY),
                    "Department.Id": str(DEPT),
                    "UniqOrderId.Id": str(UUID(int=1000 + index)),
                    "OrderNum": index + 1,
                    "ItemSaleEventDiscountType": "Discount",
                    "DishDiscountSumInt": 2,
                    "DiscountSum": Decimal("0.2"),
                }
                for index in range(150)
            ]
        ]

    monkeypatch.setattr(sales, "collect_reports", collect)
    details = repo.warehouse_sales.discount_details(scope(A, B), query)
    assert details["kind"] == "orders" and details["total"] == 150
    assert len(details["rows"]) == 100
    assert details["reconciliation"]["matched"] is True
    assert all(item["topology"] is None for item in details["rows"])
    next_page = repo.warehouse_sales.discount_details(
        scope(A, B), query.model_copy(update={"offset": 100})
    )
    assert len(next_page["rows"]) == 50 and len(requests) == 1
    items = repo.warehouse_sales.discount_details(
        scope(A, B), query.model_copy(update={"order_id": UUID(int=1000)})
    )
    assert items["kind"] == "items" and items["total"] == 2
    assert items["reconciliation"]["matched"] is True
    assert len(requests) == 2
    with pytest.raises(HTTPException) as error:
        repo.warehouse_sales.discount_details(scope(A), query)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as error:
        repo.warehouse_sales.discount_details(
            scope(A, B), query.model_copy(update={"order_id": UUID(int=9999)})
        )
    assert error.value.status_code == 404 and len(requests) == 2


def test_discount_context_expiry_is_explicit_and_cannot_use_venue_history(provider):
    from app.schemas.sales_drilldown import DiscountDetailsQuery

    repo, _, _ = provider
    now = [0]
    repo.warehouse_sales.clock = lambda: now[0]
    row = repo.sales(scope(A), "discounts", DAY, DAY)["rows"][0]
    query = DiscountDetailsQuery(report_id=row["report_id"], ordinal=row["ordinal"])
    now[0] = 1800
    with pytest.raises(HTTPException) as error:
        repo.warehouse_sales.discount_details(scope(A), query)
    assert error.value.status_code == 410
